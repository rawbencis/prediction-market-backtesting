"""T1: Lighter x Polymarket sequential arb (> 4c), one day at a time.

Frozen rule (2026-11-12 18:50 UTC) is the `S4 sequential` block of ~/lighter-shadow/lighter_eval.py
(commit 1348b2e), reused here unchanged through `market_metrics`. This driver only changes how
data is loaded (one UTC day of windows at a time, hourly files filtered as they are read) and
adds descriptive rows computed by `ext()` (event-time delays that need not sit on the 250 ms grid).

  meta  --days D1 D2            Gamma winners + fee rate per BTC/ETH 15m window -> OUT/pm_meta.csv
  day   --day D [--fee-mode M]  per-window rows -> OUT/<mode>/<day>.pkl
  agg   --since D [--until D]   aggregate per-day files
fee modes: frozen07 = the evaluator's 0.07*p*(1-p) on Polymarket (used for the interim reproduction);
           inforce = each window's Gamma feeSchedule rate.
"""

import argparse
import csv
import json
import pickle
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/ubuntu/lighter-shadow")
import lighter_eval as le  # noqa: E402

LT_DIR, PM_DIR = Path("/home/ubuntu/lighter-data"), Path("/home/ubuntu/shadow-rec")
OUT = Path(__file__).resolve().parent / "out" / "t1"
ASSETS = (0, 1)  # btc, eth
TH = 0.04
# descriptive delay grid (ms): (Lighter match after trigger, Polymarket hedge after the Lighter match)
DELAYS = ((500, 500), (250, 250), (1000, 1000), (2000, 2000), (500, 1000), (1000, 500), (300, 500), (140, 500))
MAKER = 712772


def utc(d):
    return int(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def load_hours(root, kind, dt, d, keep):
    """Rows of kind for windows of UTC day d: the day's hourly files plus the previous day's 23h and next day's 00h."""
    files = [root / str(d - timedelta(days=1)) / f"{kind}-23.bin"]
    files += [root / str(d) / f"{kind}-{h:02d}.bin" for h in range(24)]
    files += [root / str(d + timedelta(days=1)) / f"{kind}-00.bin"]
    out = []
    for f in files:
        if not f.exists():
            continue
        x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
        x = x[keep(x)]
        if len(x):
            out.append(x.copy())
        del x
    return np.concatenate(out) if out else np.zeros(0, dt)


def read_meta():
    m = {}
    p = OUT / "pm_meta.csv"
    if p.exists():
        with p.open() as f:
            for a, e, w, rate, ft in csv.reader(f):
                m[(a, int(e))] = (float(w) if w != "" else np.nan, float(rate) if rate != "" else np.nan, ft)
    return m


def cmd_meta(a):
    OUT.mkdir(parents=True, exist_ok=True)
    have = read_meta()
    d0, d1 = date.fromisoformat(a.days[0]), date.fromisoformat(a.days[1])
    now = time.time()
    keys = [(nm, e) for nm in ("btc", "eth") for e in range(utc(d0), utc(d1) + 86400, 900)
            if e + 900 + 600 < now and not ((nm, e) in have and have[(nm, e)][0] == have[(nm, e)][0])]
    print(f"{len(keys)} windows to fetch")
    for i in range(0, len(keys), 40):
        slugs = {f"{nm}-updown-15m-{e}": (nm, e) for nm, e in keys[i:i + 40]}
        url = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(f"slug={s}" for s in slugs)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
                    rows = json.load(r)
                break
            except Exception as ex:  # noqa: BLE001
                print("gamma retry", ex)
                time.sleep(5)
        else:
            continue
        for m in rows:
            if m.get("slug") not in slugs:
                continue
            try:
                names = [n.upper() for n in json.loads(m["outcomes"])]
                pr = [float(v) for v in json.loads(m["outcomePrices"])]
                w = float(names[pr.index(max(pr))] == "UP") if max(pr) >= 0.99 else np.nan
            except (KeyError, ValueError, TypeError):
                w = np.nan
            fs = m.get("feeSchedule") or {}
            have[slugs[m["slug"]]] = (w, float(fs.get("rate", np.nan)), m.get("feeType") or "")
        time.sleep(0.6)
    with (OUT / "pm_meta.csv").open("w", newline="") as f:
        csv.writer(f).writerows((k[0], k[1], "" if v[0] != v[0] else v[0], "" if v[1] != v[1] else v[1], v[2]) for k, v in sorted(have.items()))
    print(f"meta rows {len(have)}")


def ext(L, T, P, W, e, fpm, tf=15):
    """Event-time sequential arb for the descriptive delay grid. Same trigger as the frozen rule (> TH after both fees,
    min(Lighter size, Polymarket hedge size) >= 5 on the 250 ms grid); Lighter leg matched as of trigger + d1 at the trigger
    price or better with >= 5 shown; hedge as of + d2 at Polymarket's prevailing price. Returns one row per fill."""
    fl = le.fl
    g = np.arange(e * 1_000_000 + 5_000_000, (e + tf * 60) * 1_000_000, le.STEP)
    end = g[-1]

    def lbook(t):
        i = le.asof(L["l"], t)
        ii = np.maximum(i, 0)
        lb, la = L["b1"][ii] / 1e4, L["k1"][ii] / 1e4
        return lb, la, L["b1s"][ii], L["k1s"][ii], (i >= 0) & (lb > 0) & (la > lb)

    def pbook(t):
        j = le.asof(P["l"], t)
        jj = np.maximum(j, 0)
        pb, pa = P["b"][jj] / 1e4, P["k"][jj] / 1e4
        return pb, pa, P["bs"][jj], P["ks"][jj], (j >= 0) & (pb > 0) & (pa > pb)

    lb, la, lbs, las, vl = lbook(g)
    pb, pa, pbs, pas, vp = pbook(g)
    v = vl & vp
    ea = 1 - (la + (1 - pb) + fl(la) + fpm(1 - pb))
    eb = 1 - ((1 - lb) + pa + fl(lb) + fpm(pa))
    tt_, tb_ = T["T"].astype(np.int64) * 1000, T["buy"].astype(bool)
    rows, trig = [], defaultdict(int)
    for d1, d2 in DELAYS:
        D1, D2 = d1 * 1000, d2 * 1000
        for d, edge, lpx, lsz, hsz in (("a", ea, la, las, pbs), ("b", eb, lb, lbs, pas)):
            nxt = -1
            for i in np.flatnonzero(v & (edge > TH) & (np.minimum(lsz, hsz) >= le.SIZE)):
                ti = g[i]
                if ti < nxt:
                    continue
                tj, tk = ti + D1, ti + D1 + D2
                if tk > end:
                    break
                trig[(d1, d2)] += 1
                nxt = tj
                xb, xa, xbs, xas, xv = (z[0] for z in lbook(np.array([tj])))
                px_j, sz_j = (xa, xas) if d == "a" else (xb, xbs)
                if not (xv and sz_j >= le.SIZE and (px_j <= lpx[i] if d == "a" else px_j >= lpx[i])):
                    continue
                nxt = tj + le.COOL * le.STEP
                hb, ha, hbs, has, hv = (z[0] for z in pbook(np.array([tk])))
                qty = min(float(sz_j), float(hbs if d == "a" else has), le.CAP)
                row = dict(d1=d1, d2=d2, side=d, t_rel=(tj - e * 1_000_000) / 1e6, edge0=float(edge[i]), hedged=False)
                if not hv or qty < le.SIZE:
                    rows.append(row)
                    continue
                hp = (1 - hb) if d == "a" else ha
                pnl = (1 - px_j - hp if d == "a" else px_j - hp) - fl(px_j) - fpm(hp)
                row.update(hedged=True, qty=qty, pnl=float(pnl), lpx=float(px_j), hp=float(hp), small=float(sz_j) <= 30)
                if (d1, d2) == (500, 500):
                    row["raced"] = bool(np.any((tt_ >= ti) & (tt_ <= tj + 1_000_000) & (tb_ == (d == "a"))))
                    for chk in (300, 500):
                        yb, ya, ybs, yas, yv = (z[0] for z in lbook(np.array([ti + chk * 1000])))
                        p_, s_ = (ya, yas) if d == "a" else (yb, ybs)
                        row[f"disp{chk}"] = bool(yv and s_ >= le.SIZE and (p_ <= lpx[i] if d == "a" else p_ >= lpx[i]))
                    # maker proxy: nearest recorded Lighter trade on the same side within 60 s at the trigger price or better
                    side_buy = d == "a"
                    m = (tb_ == side_buy) & (np.abs(tt_ - tj) <= 60_000_000)
                    m &= (T["p"] / 1e4 <= lpx[i] + 1e-9) if side_buy else (T["p"] / 1e4 >= lpx[i] - 1e-9)
                    if m.any():
                        k = np.flatnonzero(m)[np.argmin(np.abs(tt_[m] - tj))]
                        row["maker712772"] = int(T["aa"][k] if side_buy else T["ba"][k]) == MAKER
                    # settlement: unhedged-free check of the pair (should be +1 - cost whatever the outcome if both venues agree)
                rows.append(row)
    return rows, dict(trig)


def cmd_day(a):
    d = date.fromisoformat(a.day)
    meta = read_meta()
    out = OUT / a.fee_mode
    out.mkdir(parents=True, exist_ok=True)
    t0, t1 = utc(d), utc(d) + 86400
    LB = load_hours(LT_DIR, "lb", le.LB_DT, d, lambda x: (x["tf"] == 15) & (x["a"] <= 1) & (x["e"] >= t0) & (x["e"] < t1))
    LT = load_hours(LT_DIR, "lt", le.LT_DT, d, lambda x: (x["tf"] == 15) & (x["a"] <= 1) & (x["e"] >= t0) & (x["e"] < t1))
    _, first = np.unique(LT["id"], return_index=True)
    LT = LT[np.sort(first)]
    PM = load_hours(PM_DIR, "pm", le.PM_DT, d, lambda x: (x["tf"] == 15) & (x["a"] <= 1) & (x["e"] >= t0) & (x["e"] < t1))
    mk, lo = {}, {}
    with (LT_DIR / "lighter_markets.csv").open() as f:
        mk = {int(r[3]): (r[0], int(r[2])) for r in csv.reader(f) if len(r) >= 4}
    with (LT_DIR / "lighter_outcomes.csv").open() as f:
        lo = {mk[int(r[0])]: r[2] for r in csv.reader(f) if len(r) >= 3 and int(r[0]) in mk}
    now_e = int(LB["l"].max() // 1_000_000) if len(LB) else 0
    keys = sorted({(int(x), int(y)) for x, y in zip(LB["a"], LB["e"])})
    keys = [k for k in keys if k[1] + 900 < now_e and (a.until_e is None or k[1] < a.until_e)]
    res = []
    for ai, e in keys:
        nm = le.lr.ASSETS[ai]
        L = np.sort(LB[(LB["a"] == ai) & (LB["e"] == e)], order="l")
        T = np.sort(LT[(LT["a"] == ai) & (LT["e"] == e)], order="l")
        P = np.sort(PM[(PM["a"] == ai) & (PM["e"] == e)], order="l")
        W, rate, ftype = meta.get((nm, e), (np.nan, np.nan, ""))
        if a.fee_mode == "frozen07":
            c = 0.07
        else:
            c = rate if rate == rate else (0.05 if (nm == "btc" and e >= 1795219140) else 0.01)
        le.fp = lambda p, c=c: c * p * (1 - p)  # noqa: E731  (market_metrics reads the module global)
        r = le.market_metrics(L, T, P, W, e)
        r.pop("spreads"), r.pop("sizes")
        tr = r.pop("trades")
        r = {k: v for k, v in r.items() if k.startswith(("seq", "both_valid", "grid", "l_valid", "arb_"))}
        fills, trig = ext(L, T, P, W, e, le.fp) if len(P) else ([], {})
        mshare = (sum(t["s"] for t in tr if t["maker"] == MAKER), sum(t["s"] for t in tr))
        res.append(dict(asset=nm, e=e, W=W, lcode=lo.get((nm, e)), fee=c, ftype=ftype, r=r, fills=fills, trig=trig,
                        maker_sh=mshare, npm=len(P), nl=len(L)))
    with (out / f"{d}.pkl").open("wb") as f:
        pickle.dump(res, f)
    print(f"{d} {a.fee_mode}: {len(res)} windows, PM rows {len(PM)}, Lighter rows {len(LB)}")


def ci_day(x, days, B=4000, seed=0):
    x = np.asarray(x, float)
    if len(x) < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    t = x[rng.integers(0, len(x), (B, len(x)))].sum(1) / days
    return float(np.percentile(t, 5)), float(np.percentile(t, 95))


def load(mode, since, until):
    rows = []
    for p in sorted((OUT / mode).glob("*.pkl")):
        if since <= p.stem <= until:
            rows += pickle.load(p.open("rb"))
    return rows


def cmd_agg(a):
    rows = load(a.fee_mode, a.since, a.until)
    if a.until_e:
        rows = [w for w in rows if w["e"] < a.until_e]
    print(f"mode {a.fee_mode}, {a.since}..{a.until}: {len(rows)} windows")
    # settlement agreement (Lighter code mapped by majority vs Polymarket winner)
    tab = defaultdict(lambda: [0, 0])
    for w in rows:
        if w["lcode"] is not None and w["W"] == w["W"]:
            tab[w["lcode"]][int(w["W"])] += 1
    n = sum(sum(v) for v in tab.values())
    print(f"settlement: Lighter code vs PM winner {dict(tab)}; agreement {sum(max(v) for v in tab.values()) / max(n, 1):.4f} over {n}")
    for nm in ("btc", "eth"):
        ws = [w for w in rows if w["asset"] == nm]
        days = len(ws) / 96
        key = "seq0.04"
        usd = [w["r"].get(f"{key}_usd", 0.0) for w in ws]
        sh = sum(w["r"].get(f"{key}_sh", 0.0) for w in ws)
        f_ = sum(w["r"].get(f"{key}_fill", 0.0) for w in ws)
        late = sum(w["r"].get(f"{key}_late_usd", 0.0) for w in ws)
        lo_, hi_ = ci_day(usd, days)
        print(f"\n{nm.upper()} {len(ws)} windows ({days:.2f} d). frozen 500+500: fills {f_:.0f} ({f_ / max(days, 1e-9):.1f}/d), "
              f"{100 * sum(usd) / max(sh, 1e-9):+.2f}c/share, ${sum(usd) / max(days, 1e-9):+.1f}/day [90% {lo_:+.1f},{hi_:+.1f}], "
              f"last 2 min share of $ {late / sum(usd) if sum(usd) else float('nan'):.2f}")
        for l1, l2 in le.SEQ_LAT[1:]:
            k2 = f"{key}_{l1}_{l2}"
            u = [w["r"].get(f"{k2}_usd", 0.0) for w in ws]
            s2 = sum(w["r"].get(f"{k2}_sh", 0.0) for w in ws)
            lo2, hi2 = ci_day(u, days)
            print(f"   grid {l1 * 250}+{l2 * 250}: {100 * sum(u) / max(s2, 1e-9):+.2f}c/share ${sum(u) / max(days, 1e-9):+.1f}/day [{lo2:+.1f},{hi2:+.1f}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("meta")
    p.add_argument("--days", nargs=2, required=True)
    p = sp.add_parser("day")
    p.add_argument("--day", required=True)
    p.add_argument("--fee-mode", default="inforce", choices=("frozen07", "inforce"))
    p.add_argument("--until-e", type=int, default=None)
    p = sp.add_parser("agg")
    p.add_argument("--since", required=True)
    p.add_argument("--until", default="9999")
    p.add_argument("--fee-mode", default="inforce", choices=("frozen07", "inforce"))
    p.add_argument("--until-e", type=int, default=None)
    a = ap.parse_args()
    {"meta": cmd_meta, "day": cmd_day, "agg": cmd_agg}[a.cmd](a)


if __name__ == "__main__":
    main()
