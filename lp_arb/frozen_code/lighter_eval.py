"""Evaluate Lighter shadow recordings (lighter_rec.py), alone and against Polymarket (shadow_rec.py pm files).

report --lt DIR [--pm DIR]
  S1 Lighter book: spread, size at best
  S2 flow: trades per window, who makes / takes, maker result to settlement and against Polymarket's mid
  S3 settlement: Lighter outcome vs Polymarket winner
  S4 cross-venue taker arb: Up on one venue + Down on the other for < $1 after both fees, still there 500 ms later
     sequential version: Lighter leg first (limit at the trigger price, matches 500 ms later), then the
     Polymarket hedge 500 ms after that at whatever its ask is by then; 5 s pause after each fill
  S5 stale-quote taker on Lighter: Polymarket mid as fair value, 500 ms to match; result against Polymarket's
     mid 5 s and 30 s later, and to settlement
  LL which book moves first: correlation of 1 s mid changes, Lighter vs Polymarket, at leads/lags up to 2 s
  S6 tighter maker on Lighter: quote Polymarket mid +- h; filled by recorded taker trades it would have
     price-improved; result unhedged (to settlement) and hedged on Polymarket as taker 300 ms later
Clock: the recorder's local receive time for both venues (same box). Sizes capped at CAP shares.
"""

import argparse
import csv
import json
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import lighter_rec as lr

LB_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4")]
                 + [(f"{s}{i}{f}", t) for s in "bk" for i in (1, 2, 3) for f, t in (("", "<u2"), ("s", "<f4"))])
LT_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("p", "<u2"), ("s", "<f4"),
                  ("buy", "u1"), ("aa", "<i8"), ("ba", "<i8"), ("id", "<i8")])
LC_DT = np.dtype([("l", "<i8"), ("ts", "<i8"), ("a", "u1"), ("c", "<f8")])
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])  # shadow_rec pm rows
assert LB_DT.itemsize == lr.LB_REC.size and LT_DT.itemsize == lr.LT_REC.size and LC_DT.itemsize == lr.LC_REC.size
STEP = 250_000
LAT = 2  # grid steps (500 ms) from seeing a quote to matching against it
SIZE, CAP = 5.0, 200.0
THETA = (0.02, 0.04, 0.06)
HS = (0.01, 0.02, 0.03, 0.05)
QUOTE_LAG, HEDGE_LAG = 150_000, 300_000
SEQ_TH = (0.0, 0.02, 0.04)  # minimum pair discount (after fees) to start the sequential arb
COOL = 20  # grid steps (5 s) before the next sequential arb in the same direction
# (grid steps until the Lighter leg matches, further steps until the Polymarket hedge matches); first = base case
SEQ_LAT = ((2, 2), (1, 1), (2, 4), (4, 2), (4, 4), (8, 8))
LAGS = range(-8, 9)  # grid steps; positive = Polymarket moves after Lighter
RNG = np.random.default_rng(0)


def later(x, k):
    """x shifted so that out[t] = x[t + k]; nan where that falls outside."""
    out = np.full(len(x), np.nan)
    if k >= 0:
        out[: len(x) - k] = x[k:]
    else:
        out[-k:] = x[:k]
    return out


def ci(x, days, B=2000):
    """90% interval of the per-day total, resampling windows."""
    x = np.asarray(x, float)
    if len(x) < 5:
        return np.nan, np.nan
    t = x[RNG.integers(0, len(x), (B, len(x)))].sum(1) / days
    return float(np.percentile(t, 5)), float(np.percentile(t, 95))


def fl(p):
    return 0.01 * p * (1 - p)  # Lighter taker fee per share


def fp(p):
    return 0.07 * p * (1 - p)  # Polymarket taker fee per share


def read(root, kind, dt):
    parts = [np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize) for f in sorted(root.glob(f"????-??-??/{kind}-*.bin"))]
    return np.concatenate(parts) if parts else np.zeros(0, dt)


def asof(t, q):
    return np.searchsorted(t, q, "right") - 1


def episodes(trig, pnl_exec, ok_exec):
    """First trigger of each run; executes LAT steps later. -> (n triggers, n fills, sum pnl)"""
    n = f = 0
    tot, i, N = 0.0, 0, len(trig)
    while i < N:
        if trig[i]:
            n += 1
            j = i + LAT
            if j < N and ok_exec[j]:
                f += 1
                tot += float(pnl_exec[j])
            while i < N and trig[i]:
                i += 1
        else:
            i += 1
    return n, f, tot


def market_metrics(L, T, P, W, e, tf=15):
    """L, T, P: rows of one (asset, epoch), sorted by local time. W: 1.0 if Up won, 0.0, or nan."""
    r = defaultdict(float)
    g = np.arange(e * 1_000_000 + 5_000_000, (e + tf * 60) * 1_000_000, STEP)
    i = asof(L["l"], g)
    ii = np.maximum(i, 0)
    lb, la = L["b1"][ii] / 1e4, L["k1"][ii] / 1e4
    lbs, las = L["b1s"][ii], L["k1s"][ii]
    vl = (i >= 0) & (lb > 0) & (la > lb)
    r["grid"], r["l_valid"] = len(g), vl.sum()
    sp = (la - lb)[vl]
    r["spreads"], r["sizes"] = sp, np.minimum(lbs, las)[vl]
    havep = len(P) > 0
    if havep:
        j = asof(P["l"], g)
        jj = np.maximum(j, 0)
        pb, pa, pbs, pas = P["b"][jj] / 1e4, P["k"][jj] / 1e4, P["bs"][jj], P["ks"][jj]
        vp = (j >= 0) & (pb > 0) & (pa > pb)
        v = vl & vp
        r["both_valid"] = v.sum()
        mid = (pb + pa) / 2
        # S4: a = Lighter Up + Polymarket Down; b = Lighter Down + Polymarket Up
        ea = 1 - (la + (1 - pb) + fl(la) + fp(1 - pb))
        eb = 1 - ((1 - lb) + pa + fl(lb) + fp(pa))
        sa, sb = np.minimum(las, pbs), np.minimum(lbs, pas)
        r["arb_time"] = (v & (np.maximum(ea, eb) > 0)).sum()
        r["arb_time_gross"] = (v & ((la < pb) | (lb > pa))).sum()
        for ed, sz in ((ea, sa), (eb, sb)):
            ok = v & (ed > 0) & (sz >= SIZE)
            n, f, tot = episodes(ok, ed * np.minimum(sz, CAP), ok)
            r["arb_trig"] += n
            r["arb_fill"] += f
            r["arb_usd"] += tot
        N = len(g)
        midv = np.where(vp, mid, np.nan)
        mid5, mid30 = later(midv, 20), later(midv, 120)
        # S4 sequential: Lighter leg first, Polymarket hedge after the fill
        tt_, tb_ = T["T"].astype(np.int64) * 1000, T["buy"].astype(bool)
        for th in SEQ_TH:
            for l1, l2 in SEQ_LAT if th > 0 else SEQ_LAT[:1]:
                key = f"seq{th}" if (l1, l2) == SEQ_LAT[0] else f"seq{th}_{l1}_{l2}"
                for d, edge, lpx, lsz, hsz in (("a", ea, la, las, pbs), ("b", eb, lb, lbs, pas)):
                    nxt = 0
                    for i in np.flatnonzero(v & (edge > th) & (np.minimum(lsz, hsz) >= SIZE)):
                        if i < nxt:
                            continue
                        j, k = i + l1, i + l1 + l2
                        if k >= N:
                            break
                        r[f"{key}_trig"] += 1
                        nxt = j
                        if not (vl[j] and lsz[j] >= SIZE and (lpx[j] <= lpx[i] if d == "a" else lpx[j] >= lpx[i])):
                            continue  # the Lighter quote was pulled or moved away: nothing bought
                        nxt = j + COOL
                        qty = min(float(lsz[j]), float(hsz[k]), CAP)
                        if not vp[k] or qty < SIZE:
                            r[f"{key}_nohedge"] += 1
                            continue
                        hp = (1 - pb[k]) if d == "a" else pa[k]
                        pnl = (1 - lpx[j] - hp if d == "a" else lpx[j] - hp) - fl(lpx[j]) - fp(hp)
                        r[f"{key}_fill"] += 1
                        r[f"{key}_sh"] += qty
                        r[f"{key}_usd"] += pnl * qty
                        r[f"{key}_pos"] += pnl > 0
                        if key == f"seq{th}":
                            if g[j] - e * 1_000_000 >= (tf * 60 - 120) * 1_000_000:
                                r[f"{key}_late_usd"] += pnl * qty
                            r[f"{key}_small_usd"] += pnl * qty if lsz[j] <= 30 else 0.0
                            if np.any((tt_ >= g[i]) & (tt_ <= g[j] + 1_000_000) & (tb_ == (d == "a"))):
                                r[f"{key}_raced"] += 1
        # S5: Lighter taker against stale quotes, Polymarket mid as fair
        for th in THETA:
            for side, edge, px_, sz in ((1, mid - la - fl(la), la, las), (-1, lb - mid - fl(lb), lb, lbs)):
                ok = v & (edge > th) & (sz >= SIZE)
                q = np.minimum(sz, CAP)
                n, f, tot = episodes(ok, edge * q, ok)
                r[f"sn{th}_trig"] += n
                r[f"sn{th}_fill"] += f
                r[f"sn{th}_model"] += tot
                for nm, m_ in (("mo5", mid5), ("mo30", mid30)):
                    r[f"sn{th}_{nm}"] += episodes(ok, np.nan_to_num((side * (m_ - px_) - fl(px_)) * q), ok)[2]
                if W == W:
                    r[f"sn{th}_settle"] += episodes(ok, (side * (W - px_) - fl(px_)) * q, ok)[2]
        # LL: which mid moves first (1 s changes; Lighter only while its spread is <= 10c)
        lm = np.where(v & ((la - lb) <= 0.1001), (la + lb) / 2, np.nan)
        dl, dp = later(lm, 4) - lm, later(midv, 4) - midv
        r["ll_xx"], r["ll_yy"] = np.nansum(dl * dl), np.nansum(dp * dp)
        for k in LAGS:
            r[f"ll{k}"] = np.nansum(dl * later(dp, k))
    # S2 / S6 trades
    tr = []
    for t in T:
        tl = int(t["T"]) * 1000 if t["T"] > 0 else int(t["l"])  # trade time; snapshot trades arrive late
        p, s, buy = t["p"] / 1e4, float(t["s"]), bool(t["buy"])
        sgn = 1.0 if buy else -1.0  # maker sold Up when the taker bought
        d = dict(p=p, s=s, buy=buy, maker=int(t["aa"] if buy else t["ba"]), taker=int(t["ba"] if buy else t["aa"]),
                 fee=fl(p) * s, mk_settle=sgn * (p - W) * s if W == W else np.nan, mk_mid=np.nan, mk_mo5=np.nan,
                 mk_mo30=np.nan, mk_mo120=np.nan)
        if havep:
            for key, dt in (("mk_mid", 0), ("mk_mo5", 5_000_000), ("mk_mo30", 30_000_000), ("mk_mo120", 120_000_000)):
                k = asof(P["l"], tl + dt)
                if k >= 0 and P["b"][k] > 0 and P["k"][k] > P["b"][k]:
                    d[key] = sgn * (p - (P["b"][k] + P["k"][k]) / 2e4) * s
            kq, kh = asof(P["l"], tl - QUOTE_LAG), asof(P["l"], tl + HEDGE_LAG)
            if kq >= 0 and kh >= 0 and P["b"][kq] > 0 and P["k"][kq] > P["b"][kq] and P["b"][kh] > 0 and P["k"][kh] > P["b"][kh]:
                m0 = (P["b"][kq] + P["k"][kq]) / 2e4
                q = min(s, CAP)
                k30 = asof(P["l"], tl + 30_000_000)
                m30 = (P["b"][k30] + P["k"][k30]) / 2e4 if P["b"][k30] > 0 and P["k"][k30] > P["b"][k30] else np.nan
                for h in HS:
                    if buy:  # we sell Up at A, hedge by buying Up on Polymarket
                        A = np.ceil((m0 + h) * 100 - 1e-9) / 100
                        if A < p - 1e-9:
                            ha = P["k"][kh] / 1e4
                            r[f"mm{h}_n"] += 1
                            r[f"mm{h}_sh"] += q
                            r[f"mm{h}_hedged"] += (A - ha - fp(ha)) * q
                            r[f"mm{h}_mo30"] += 0 if m30 != m30 else (A - m30) * q
                            if W == W:
                                r[f"mm{h}_settle"] += (A - W) * q
                    else:  # we buy Up at B, hedge by buying Down on Polymarket at 1 - bid
                        B = np.floor((m0 - h) * 100 + 1e-9) / 100
                        if B > p + 1e-9:
                            hb = P["b"][kh] / 1e4
                            r[f"mm{h}_n"] += 1
                            r[f"mm{h}_sh"] += q
                            r[f"mm{h}_hedged"] += (hb - B - fp(1 - hb)) * q
                            r[f"mm{h}_mo30"] += 0 if m30 != m30 else (m30 - B) * q
                            if W == W:
                                r[f"mm{h}_settle"] += (W - B) * q
        tr.append(d)
    r["trades"] = tr
    for key in ("mk_settle", "mk_mid", "mk_mo5", "mk_mo30", "mk_mo120"):
        r[f"w_{key}"] = float(np.nansum([t[key] for t in tr])) if tr else 0.0
    return r


def pm_winners(cache, keys, fetch=True):
    """keys: (asset_name, epoch). -> {(asset, epoch): 1.0 / 0.0}; cached in a csv."""
    have = {}
    if cache.exists():
        with cache.open() as f:
            have = {(a, int(e)): float(w) for a, e, w in csv.reader(f)}
    todo = [k for k in keys if k not in have] if fetch else []
    for i in range(0, len(todo), 40):
        slugs = {f"{a}-updown-15m-{e}": (a, e) for a, e in todo[i : i + 40]}
        try:
            url = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(f"slug={s}" for s in slugs)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "updown-shadow"}), timeout=30) as resp:
                rows = json.load(resp)
        except Exception as ex:  # noqa: BLE001
            print(f"  gamma failed: {ex!r}")
            break
        for m in rows:
            try:
                names = [n.upper() for n in json.loads(m["outcomes"])]
                pr = [float(v) for v in json.loads(m["outcomePrices"])]
                if m["slug"] in slugs and max(pr) >= 0.99:
                    have[slugs[m["slug"]]] = float(names[pr.index(max(pr))] == "UP")
            except (KeyError, ValueError, TypeError):
                continue
    with cache.open("w", newline="") as f:
        csv.writer(f).writerows((a, e, w) for (a, e), w in sorted(have.items()))
    return have


def q(x, ps=(25, 50, 75)):
    return [round(float(v), 3) for v in np.percentile(x, ps)] if len(x) else None


def cmd_report(a):
    LB, LT = read(a.lt, "lb", LB_DT), read(a.lt, "lt", LT_DT)
    LB, LT = LB[LB["tf"] == 15], LT[LT["tf"] == 15]
    _, first = np.unique(LT["id"], return_index=True)
    LT = LT[np.sort(first)]  # a reconnect can replay recent trades
    PM = read(a.pm, "pm", PM_DT) if a.pm else np.zeros(0, PM_DT)
    PM = PM[PM["tf"] == 15]
    mk = {}
    if (a.lt / "lighter_markets.csv").exists():
        with (a.lt / "lighter_markets.csv").open() as f:
            mk = {int(r[3]): (r[0], int(r[2])) for r in csv.reader(f) if len(r) >= 4}
    lo = {}
    if (a.lt / "lighter_outcomes.csv").exists():
        with (a.lt / "lighter_outcomes.csv").open() as f:
            lo = {mk[int(r[0])]: r[2] for r in csv.reader(f) if len(r) >= 3 and int(r[0]) in mk}
    now_e = int(LB["l"].max() // 1_000_000) if len(LB) else 0
    keys = sorted({(int(x), int(y)) for x, y in zip(LB["a"], LB["e"])})
    keys = [k for k in keys if k[1] + 900 < now_e]  # finished windows only
    if a.since:
        t0 = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp())
        keys = [k for k in keys if k[1] >= t0]
        lo = {k: v for k, v in lo.items() if k[1] >= t0}
        print(f"only windows starting at or after {a.since} UTC")
    win = pm_winners(a.lt / "pm_winners.csv", [(lr.ASSETS[x], e) for x, e in keys], fetch=not a.no_gamma)
    # decode Lighter's outcome code against Polymarket winners
    tab = defaultdict(lambda: [0, 0])
    for k, code in lo.items():
        if k in win:
            tab[code][int(win[k])] += 1
    up_code = {c: v[1] >= v[0] for c, v in tab.items()}
    print(f"markets: {len(keys)} finished 15m windows, {len({e for _, e in keys})} distinct windows "
          f"({len({e for _, e in keys}) / 96:.2f} days); Polymarket rows: {len(PM)}; Polymarket winners: {len(win)}")
    print("\nS3 settlement: Lighter outcome code vs Polymarket winner [Down, Up]:", {c: v for c, v in tab.items()} or "no overlap yet")
    if tab:
        agree = sum(max(v) for v in tab.values()) / sum(sum(v) for v in tab.values())
        print(f"   agreement {agree:.4f} over {sum(sum(v) for v in tab.values())} windows (a real cross-venue arb needs ~1.0)")
    for ai in sorted({k[0] for k in keys}):
        name = lr.ASSETS[ai]
        ks = [k for k in keys if k[0] == ai]
        days = max(len(ks) / 96, 1e-9)
        A, per = defaultdict(float), defaultdict(list)
        sp, sz, trades = [], [], []
        Lb, Lt, Pm = LB[LB["a"] == ai], LT[LT["a"] == ai], PM[PM["a"] == ai]
        for _, e in ks:
            L = np.sort(Lb[Lb["e"] == e], order="l")
            T = np.sort(Lt[Lt["e"] == e], order="l")
            P = np.sort(Pm[Pm["e"] == e], order="l")
            W = win.get((name, e), np.nan)
            if W != W and (name, e) in lo and lo[(name, e)] in up_code:
                W = float(up_code[lo[(name, e)]])
            r = market_metrics(L, T, P, W, e)
            sp.append(r.pop("spreads"))
            sz.append(r.pop("sizes"))
            trades += r.pop("trades")
            for k2, v in r.items():
                A[k2] += v
                per[k2].append(float(v))
        sp, sz = np.concatenate(sp) if sp else np.zeros(0), np.concatenate(sz) if sz else np.zeros(0)
        print(f"\n===== {name.upper()} 15m: {len(ks)} windows ({days:.2f} days) =====")
        print(f"S1 book: two-sided {A['l_valid'] / max(A['grid'], 1):.3f} of the time; spread c p25/50/75 {[round(100 * x, 1) for x in q(sp) or []]}; "
              f"share <=2c {np.mean(sp <= 0.0201) if len(sp) else 0:.3f} <=5c {np.mean(sp <= 0.0501) if len(sp) else 0:.3f} <=10c {np.mean(sp <= 0.1001) if len(sp) else 0:.3f}; "
              f"size at best p25/50/75 {q(sz)}")
        n = len(trades)
        sh = sum(t["s"] for t in trades)
        usd = sum(t["s"] * t["p"] for t in trades)
        print(f"S2 flow: {n} trades, {n / max(len(ks), 1):.1f}/window; {sh / max(len(ks), 1):.0f} shares and ${usd / max(len(ks), 1):.0f} per window "
              f"(${usd / days:.0f}/day); taker buys {np.mean([t['buy'] for t in trades]) if n else 0:.2f}; "
              f"taker fees ${sum(t['fee'] for t in trades) / days:.2f}/day; distinct takers {len({t['taker'] for t in trades})}, makers {len({t['maker'] for t in trades})}")
        nn = lambda v: float(np.nansum(v))  # noqa: E731
        print(f"   all makers together, $/day: to settlement {nn([t['mk_settle'] for t in trades]) / days:+.2f}; "
              f"vs Polymarket mid at the trade {nn([t['mk_mid'] for t in trades]) / days:+.2f}; vs mid 5 s later {nn([t['mk_mo5'] for t in trades]) / days:+.2f}")
        cps = lambda k: 100 * A[f"w_{k}"] / max(sh, 1e-9)  # noqa: E731
        iv = lambda k: "[{:+.0f},{:+.0f}]".format(*ci(per[k], days))  # noqa: E731
        print(f"   makers, cents per share: at the trade {cps('mk_mid'):+.2f}, after 5 s {cps('mk_mo5'):+.2f}, 30 s {cps('mk_mo30'):+.2f}, "
              f"120 s {cps('mk_mo120'):+.2f}, to settlement {cps('mk_settle'):+.2f}; 90% range $/day: settlement {iv('w_mk_settle')}, "
              f"30 s {iv('w_mk_mo30')}")
        for role in ("maker", "taker"):
            agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0, 0.0])
            for t in trades:
                g_ = agg[t[role]]
                sign = 1 if role == "maker" else -1
                g_[0] += t["s"]
                g_[1] += 0 if t["mk_settle"] != t["mk_settle"] else sign * t["mk_settle"] - (t["fee"] if role == "taker" else 0)
                g_[2] += 0 if t["mk_mid"] != t["mk_mid"] else sign * t["mk_mid"]
                g_[3] += 1
                g_[4] += 0 if t["mk_mo30"] != t["mk_mo30"] else sign * t["mk_mo30"]
            top = sorted(agg.items(), key=lambda kv: -kv[1][0])[:5]
            print(f"   top {role}s (account: shares, trades, $ to settlement{' after fee' if role == 'taker' else ''}, $ vs Polymarket mid at the trade, "
                  f"cents/share vs mid 30 s later):")
            for acc, (s_, ps, pm_, c, m30) in top:
                print(f"      {acc}: {s_:.0f} sh ({s_ / max(sh, 1e-9):.0%}), {c} trades, {ps:+.2f}, {pm_:+.2f}, {100 * m30 / max(s_, 1e-9):+.2f}c")
        if not A["both_valid"]:
            print("S4-S6: no overlapping Polymarket rows (pass --pm with the shadow_rec output folder)")
            continue
        bv = A["both_valid"]
        print(f"S4 cross-venue taker arb: both books valid {bv / max(A['grid'], 1):.3f} of the time; pair < $1 after fees {A['arb_time'] / bv:.4f} of that "
              f"(before fees {A['arb_time_gross'] / bv:.4f}); episodes {A['arb_trig'] / days:.1f}/day, still there after 500 ms {A['arb_fill'] / days:.1f}/day, ${A['arb_usd'] / days:+.2f}/day")
        for th in SEQ_TH:
            f_, s_ = A[f"seq{th}_fill"], A[f"seq{th}_sh"]
            print(f"S4 sequential, start at > {int(th * 100)}c: tries {A[f'seq{th}_trig'] / days:.0f}/day, Lighter leg filled {f_ / days:.0f}/day "
                  f"({s_ / days:.0f} shares/day), hedged pair profitable {A[f'seq{th}_pos'] / max(f_, 1):.2f} of fills, "
                  f"{100 * A[f'seq{th}_usd'] / max(s_, 1e-9):+.2f}c/share, ${A[f'seq{th}_usd'] / days:+.2f}/day {iv(f'seq{th}_usd')}")
            if th > 0:
                print(f"     of that $: last 2 min of the window {A[f'seq{th}_late_usd'] / days:+.0f}/day, quotes of <= 30 shares {A[f'seq{th}_small_usd'] / days:+.0f}/day; "
                      f"a real taker hit the same side within 1 s on {A[f'seq{th}_raced'] / max(f_, 1):.2f} of fills; no hedge size {A[f'seq{th}_nohedge'] / days:.0f}/day")
                print("     by delay (Lighter match ms + hedge ms): " + "; ".join(
                    f"{l1 * 250}+{l2 * 250}: {100 * A[f'seq{th}_{l1}_{l2}_usd'] / max(A[f'seq{th}_{l1}_{l2}_sh'], 1e-9):+.2f}c, ${A[f'seq{th}_{l1}_{l2}_usd'] / days:+.0f} "
                    + iv(f"seq{th}_{l1}_{l2}_usd") for l1, l2 in SEQ_LAT[1:]))
        for th in THETA:
            print(f"S5 stale-quote taker, edge > {int(th * 100)}c vs Polymarket mid: episodes {A[f'sn{th}_trig'] / days:.1f}/day, fills {A[f'sn{th}_fill'] / days:.1f}/day, "
                  f"model ${A[f'sn{th}_model'] / days:+.2f}/day, vs mid 5 s later ${A[f'sn{th}_mo5'] / days:+.2f}, 30 s later ${A[f'sn{th}_mo30'] / days:+.2f} "
                  f"{iv(f'sn{th}_mo30')}, to settlement ${A[f'sn{th}_settle'] / days:+.2f}/day {iv(f'sn{th}_settle')}")
        for h in HS:
            print(f"S6 maker at Polymarket mid +- {int(h * 100)}c: {A[f'mm{h}_n'] / days:.1f} fills/day, {A[f'mm{h}_sh'] / days:.0f} shares/day; "
                  f"hedged on Polymarket ${A[f'mm{h}_hedged'] / days:+.2f}/day {iv(f'mm{h}_hedged')}; unhedged vs mid 30 s later ${A[f'mm{h}_mo30'] / days:+.2f} "
                  f"{iv(f'mm{h}_mo30')}; unhedged to settlement ${A[f'mm{h}_settle'] / days:+.2f}/day")
        den = np.sqrt(A["ll_xx"] * A["ll_yy"]) or 1.0
        cc = {k: A[f"ll{k}"] / den for k in LAGS}
        best = max(cc, key=cc.get)
        print(f"LL correlation of 1 s mid changes, Polymarket shifted by (s): " + ", ".join(f"{k * STEP / 1e6:+.2f}: {cc[k]:.2f}" for k in LAGS if k % 2 == 0)
              + f"; peak at {best * STEP / 1e6:+.2f} s (positive = Polymarket moves after Lighter, as seen from this box)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("report")
    p.add_argument("--lt", type=Path, required=True, help="lighter_rec output folder")
    p.add_argument("--pm", type=Path, default=None, help="shadow_rec output folder (Polymarket books)")
    p.add_argument("--no-gamma", action="store_true")
    p.add_argument("--since", default=None, help="UTC date or datetime, e.g. 2026-11-13 or 2026-11-12T19:00")
    cmd_report(ap.parse_args())


if __name__ == "__main__":
    main()
