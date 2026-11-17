"""Late-breakout taker (Study 4 rule) on the live shadow recordings.

Polymarket: 5 assets x 5m/15m (shadow_rec.py). Lighter: BTC/ETH 15m (lighter_rec.py).

Trigger (frozen 2026-11-12): a token's ask reaches a level X in {60,65,70,75,80}c that it had not
reached earlier in the window (it was below X on an earlier valid quote), within the last LATE s.
One trade per market: the earliest such trigger. Buy SIZE shares FAK, cap = trigger ask + 5c.
Primary (pre-registered): Polymarket BTC 5m, LATE 60 s, scenario tokyo, cap +5c. All else exploratory.

eval:   per recorded day -> RES/bo-{day}.csv: one row per trigger (all X, last KEEP_S seconds) with the
        fill price under every scenario x cap (empty = no fill).
report: winners from Gamma; net c/share, fill rate, $/day, day-bootstrap 90% CI; pass/kill for the primary.

Scenarios (venue exchange clock unless noted):
  pm     tokyo  lands rx + d10 + proc + hold after the trigger row (rx = that row's receive delay,
                d10 = the day's p10 receive delay as one-way order transit, hold = exchange taker hold)
         colo   1 + 1 + proc + hold;   L100/L250/L500/L1000: fixed delay (June-July backtest scale)
  lt     std / prem: the rule on Lighter's own book; rx + d10 + proc + 300 / 140 ms (taker hold by account)
  pm>lt  std / prem: Polymarket 15m trigger, bought on Lighter (local clock: PM receipt + proc + d10 + hold)
Fills: Polymarket top level only (>= SIZE shown at <= cap); Lighter walks its 3 recorded levels.
"""

import argparse
import itertools
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import lighter_eval as le
import shadow_eval as se
import shadow_rec as rec

XS = (6000, 6500, 7000, 7500, 8000)  # 1e-4 $
KEEP_S = 180
SIZE = 5.0
CAPS = {"c2": 200, "c5": 500, "c99": None}
FIXED = (100, 250, 500, 1000)
LT_HOLD = {"std": 300.0, "prem": 140.0}
LT_TAGS = (0, 1)  # btc, eth


def triggers(x, ask, ok, e, W):
    """First-ever crossings of each X within the last KEEP_S s. x: exchange ms (sorted). -> [(X, j)]"""
    vi = np.flatnonzero(ok)
    out = []
    if not len(vi):
        return out
    a = ask[vi]
    for X in XS:
        lo = np.flatnonzero(a < X)
        if not len(lo):
            continue
        hi = np.flatnonzero(a[lo[0] :] >= X)
        if not len(hi):
            continue
        j = vi[lo[0] + hi[0]]
        if (e + W - KEEP_S) * 1000 <= x[j] < (e + W) * 1000:
            out.append((X, int(j)))
    return out


def fill(levels, cap):
    """levels: [(px, size)] ascending, px 1e-4 (0 = empty). -> VWAP for SIZE shares at <= cap, else nan."""
    got, cost = 0.0, 0.0
    for p, s in levels:
        if p <= 0 or p > cap or s <= 0:
            break
        q = min(s, SIZE - got)
        got, cost = got + q, cost + q * p
        if got >= SIZE - 1e-9:
            return cost / got / 1e4
    return np.nan


def caps(a0):
    return {k: (min(a0 + c, 9900) if c is not None else 9900) for k, c in CAPS.items()}


def fills(levels_at, t_arr, close_ms, a0):
    """levels_at(t) -> levels or None. -> {cap: price or nan}"""
    lv = levels_at(t_arr) if t_arr < close_ms else None
    return {k: (fill(lv, c) if lv else np.nan) for k, c in caps(a0).items()}


def pm_window(r, d10, a):
    o = np.lexsort((r["l"], r["x"]))
    r = r[o]
    x, l = r["x"].astype(np.int64), r["l"].astype(np.int64)
    b, k = r["b"].astype(np.int64), r["k"].astype(np.int64)
    ok = (b > 0) & (k > b)
    ai, tf, e = int(r["a"][0]), int(r["tf"][0]), int(r["e"][0])
    W, close = tf * 60, (int(r["e"][0]) + tf * 60) * 1000
    books = ((k, r["ks"]), (10000 - b, r["bs"]))  # UP ask, DOWN ask (= 1 - UP bid)
    rows, sig = [], []
    for tok, (ask, sz) in enumerate(books):
        for X, j in triggers(x, ask, ok, e, W):

            def lv(t, ask=ask, sz=sz):
                i = int(np.searchsorted(x, t, "right")) - 1
                return [(int(ask[i]), float(sz[i]))] if i >= 0 and ok[i] else None

            rx = l[j] / 1000 - x[j]
            scen = {
                "tokyo": x[j] + rx + d10 + a.proc_ms + a.taker_delay_ms,
                "colo": x[j] + 2 + a.proc_ms + a.taker_delay_ms,
            }
            scen.update({f"L{L}": x[j] + L for L in FIXED})
            row = {"venue": "pm", "asset": rec.ASSETS[ai], "tf": tf, "epoch": e, "tok": tok, "X": X, "t_s": x[j] / 1000 - e,
                       "a0": int(ask[j]), "spread0": int(k[j] - b[j]), "rx_ms": rx}  # fmt: skip
            for s, t in scen.items():
                for c, p in fills(lv, t, close, int(ask[j])).items():
                    row[f"{s}_{c}"] = p
            rows.append(row)
            sig.append((ai, e, tok, X, x[j] / 1000 - e, int(l[j]), int(ask[j])))
    return rows, sig


def lt_levels(L, i, tok):
    if tok == 0:
        return [(int(L[f"k{n}"][i]), float(L[f"k{n}s"][i])) for n in (1, 2, 3)]
    return [
        (10000 - int(L[f"b{n}"][i]) if L[f"b{n}"][i] > 0 else 0, float(L[f"b{n}s"][i]))
        for n in (1, 2, 3)
    ]


def lt_window(L, d10, d50, pmsig, a):
    L = L[np.lexsort((L["l"], L["x"]))]
    x, l = L["x"].astype(np.int64), L["l"].astype(np.int64)
    b1, k1 = L["b1"].astype(np.int64), L["k1"].astype(np.int64)
    ok = (b1 > 0) & (k1 > b1)
    ai, e = int(L["a"][0]), int(L["e"][0])
    W, close = 900, (e + 900) * 1000
    rows = []
    for tok, ask in enumerate((k1, 10000 - b1)):
        for X, j in triggers(x, ask, ok, e, W):

            def lv(t, tok=tok):
                i = int(np.searchsorted(x, t, "right")) - 1
                return lt_levels(L, i, tok) if i >= 0 and ok[i] else None

            rx = l[j] / 1000 - x[j]
            row = {"venue": "lt", "asset": rec.ASSETS[ai], "tf": 15, "epoch": e, "tok": tok, "X": X, "t_s": x[j] / 1000 - e,
                       "a0": int(ask[j]), "spread0": int(k1[j] - b1[j]), "rx_ms": rx}  # fmt: skip
            for s, hold in LT_HOLD.items():
                for c, p in fills(
                    lv, x[j] + rx + d10 + a.proc_ms + hold, close, int(ask[j])
                ).items():
                    row[f"{s}_{c}"] = p
            rows.append(row)
    # Polymarket signal, Lighter execution (local clock; Lighter's state at local time t is what arrived by t + d50)
    lo = np.argsort(l, kind="stable")
    ls = l[lo]
    for _, _, tok, X, t_s, lj, pa0 in pmsig:

        def lvl(t_us, tok=tok):
            i = int(np.searchsorted(ls, t_us, "right")) - 1
            return lt_levels(L, lo[i], tok) if i >= 0 and ok[lo[i]] else None

        i0 = int(np.searchsorted(ls, lj, "right")) - 1
        la0 = lt_levels(L, lo[i0], tok)[0][0] if i0 >= 0 and ok[lo[i0]] else -1
        row = {"venue": "pm>lt", "asset": rec.ASSETS[ai], "tf": 15, "epoch": e, "tok": tok, "X": X, "t_s": t_s, "a0": pa0,
                   "spread0": np.nan, "rx_ms": np.nan, "lt_a0": la0}  # fmt: skip
        for s, hold in LT_HOLD.items():
            t_us = lj + (a.proc_ms + d10 + hold + d50) * 1000
            for c, p in fills(lambda t: lvl(t * 1000), t_us / 1000, close + d50, pa0).items():
                row[f"{s}_{c}"] = p
        rows.append(row)
    return rows


def eval_day(a, day):
    nxt = (date.fromisoformat(day) + timedelta(1)).isoformat()
    pm = np.concatenate(
        [se.read(a.rec, day, "pm-", se.PM_DT), se.read(a.rec, nxt, "pm-00", se.PM_DT)]
    )
    d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
    pm = pm[(pm["e"] >= d0) & (pm["e"] < d0 + 86400)]
    rows, sigs, info = [], {}, {"day": day}
    if len(pm):
        live = (pm["x"] > 0) & (pm["k"] > 0)
        d10 = float(np.percentile(pm["l"][live] / 1000 - pm["x"][live], 10))
        info["pm_d10_ms"] = round(d10, 1)
        key = (pm["a"].astype(np.int64) << 40) | (pm["tf"].astype(np.int64) << 32) | pm["e"]
        o = np.argsort(key, kind="stable")
        pm, key = pm[o], key[o]
        cuts = np.r_[0, np.flatnonzero(np.diff(key)) + 1, len(pm)]
        for c0, c1 in itertools.pairwise(cuts):
            r, s = pm_window(pm[c0:c1], d10, a)
            rows += r
            if int(pm["tf"][c0]) == 15 and int(pm["a"][c0]) in LT_TAGS:
                sigs[(int(pm["a"][c0]), int(pm["e"][c0]))] = s
    if a.lt:
        lb = np.concatenate([_lt_read(a.lt, day), _lt_read(a.lt, nxt, "00")])
        lb = lb[(lb["tf"] == 15) & (lb["e"] >= d0) & (lb["e"] < d0 + 86400)]
        if len(lb):
            live = lb["x"] > 0
            d10, d50 = np.percentile(lb["l"][live] / 1000 - lb["x"][live], [10, 50])
            info["lt_d10_ms"], info["lt_d50_ms"] = round(float(d10), 1), round(float(d50), 1)
            key = (lb["a"].astype(np.int64) << 32) | lb["e"]
            o = np.argsort(key, kind="stable")
            lb, key = lb[o], key[o]
            cuts = np.r_[0, np.flatnonzero(np.diff(key)) + 1, len(lb)]
            for c0, c1 in itertools.pairwise(cuts):
                L = lb[c0:c1]
                rows += lt_window(
                    L, float(d10), float(d50), sigs.get((int(L["a"][0]), int(L["e"][0])), []), a
                )
    D = pd.DataFrame(rows)
    D.insert(0, "day", day)
    D.to_csv(a.res / f"bo-{day}.csv", index=False)
    info["triggers"] = {v: int((D.venue == v).sum()) for v in D.venue.unique()} if len(D) else {}
    return info


def _lt_read(root, day, hour="*"):
    parts = [np.fromfile(f, dtype=le.LB_DT, count=f.stat().st_size // le.LB_DT.itemsize)
             for f in sorted((root / day).glob(f"lb-{hour}.bin"))]  # fmt: skip
    return np.concatenate(parts) if parts else np.zeros(0, le.LB_DT)


def cmd_eval(a):
    a.res.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    days = a.days or sorted(
        p.name for p in a.rec.glob("????-??-??") if p.is_dir() and p.name < today
    )
    for d in days:
        if (a.res / f"bo-{d}.csv").exists() and not a.force:
            continue
        print(eval_day(a, d), flush=True)


# ---------------------------------------------------------------- report


def rboot(S, N, B=2000, seed=0):
    if len(S) < 2 or N.sum() == 0:
        return np.nan, np.nan
    i = np.random.default_rng(seed).integers(0, len(S), (B, len(S)))
    with np.errstate(invalid="ignore", divide="ignore"):
        m = S[i].sum(1) / N[i].sum(1)
    return tuple(np.nanpercentile(m, [5, 95]))


def stat(G, col, rate, days):
    p = G[col]
    f = p.notna()
    net = np.where(
        f,
        (G.tok == G.win).astype(float) - p.fillna(0) - rate * p.fillna(0) * (1 - p.fillna(0)),
        0.0,
    )
    S = pd.Series(net).groupby(G.day.values).sum().reindex(days, fill_value=0).to_numpy()
    N = (
        pd.Series(f.to_numpy(float))
        .groupby(G.day.values)
        .sum()
        .reindex(days, fill_value=0)
        .to_numpy()
    )
    lo, hi = rboot(S, N)
    nf = int(f.sum())
    return {"n": len(G), "fill": nf / max(len(G), 1), "net_c": 100 * net.sum() / max(nf, 1), "lo": 100 * lo, "hi": 100 * hi,
                "per_att_c": 100 * net.sum() / max(len(G), 1), "usd_day": SIZE * net.sum() / max(len(days), 1)}  # fmt: skip


MAX_RX_MS = (
    1000.0  # trigger row received > 1 s late = recorder backlog: drop the market (data hygiene)
)


def first_per_market(D, late):
    D = D[D.t_s >= D.tf * 60 - late]
    F = (
        D.sort_values(["t_s", "X"])
        .groupby(["venue", "asset", "tf", "epoch"], as_index=False)
        .head(1)
    )
    return F[~(F.rx_ms > MAX_RX_MS)]


def cmd_report(a):
    fs = sorted(a.res.glob("bo-????-??-??.csv"))
    D = pd.concat([pd.read_csv(f) for f in fs if f.stat().st_size > 1], ignore_index=True)
    D["slug"] = D.asset + "-updown-" + D.tf.astype(str) + "m-" + D.epoch.astype(str)
    Wn = se.settle(a.res / "settlements.csv", sorted(set(D.slug)), not a.no_fetch)
    D["win"] = D.slug.map({k: int(v == "DOWN") for k, v in Wn.items()})  # tok 0 = UP, 1 = DOWN
    D = D[D.win.notna() & (D.day >= a.since)]
    days = sorted(D.day.unique())
    raw = D.sort_values(["t_s", "X"]).groupby(["venue", "asset", "tf", "epoch"]).head(1)
    raw = raw[raw.t_s >= raw.tf * 60 - 60]
    print(f"recorder backlog: {int((raw.rx_ms > MAX_RX_MS).sum())} of {len(raw)} first triggers (last 60 s) "
          f"arrived > {MAX_RX_MS:.0f} ms late and are dropped; max {raw.rx_ms.max():.0f} ms")  # fmt: skip
    rate = {"pm": a.fee_rate, "lt": a.lt_fee, "pm>lt": a.lt_fee}
    print(
        f"{len(days)} settled days {days[:1]}..{days[-1:]}; taker hold {a.taker_delay_ms} ms assumed in eval"
    )
    print("units: cents/share net of taker fee; CI 90% day bootstrap; $/day at 5 shares\n")

    def line(lab, s):
        return (f"{lab:>40} n {s['n']:>5} fill {s['fill']:4.2f} net {s['net_c']:6.2f} [{s['lo']:6.2f},{s['hi']:6.2f}] "
                f"per-att {s['per_att_c']:6.2f} $/day {s['usd_day']:6.2f}")  # fmt: skip

    F60 = first_per_market(D, 60)
    P = F60[(F60.venue == "pm") & (F60.asset == "btc") & (F60.tf == 5)]
    s = stat(P, "tokyo_c5", rate["pm"], days)
    ok = (
        len(days) >= 14
        and s["n"] * s["fill"] >= 500
        and s["net_c"] >= 1
        and s["lo"] > 0
        and s["per_att_c"] > 0
    )
    kill = len(days) >= 14 and s["hi"] < 1
    print(
        "== PRIMARY (pre-registered): Polymarket BTC 5m, last 60 s, first trigger per market, tokyo, cap +5c =="
    )
    print(line("btc 5m tokyo c5", s))
    print(
        f"   status: {'PASS' if ok else 'KILL' if kill else 'running'} (needs >= 14 days, >= 500 fills, net >= 1c, CI low > 0)\n"
    )

    scen = ["tokyo", "colo"] + [f"L{L}" for L in FIXED]
    for late in (60, 180):
        F = first_per_market(D, late)
        rows = []
        for (asset, tf), G in F[F.venue == "pm"].groupby(["asset", "tf"]):
            if late == 180 and tf == 5:
                continue
            r = {"mkt": f"{asset} {tf}m", "n": len(G)}
            for sc in scen:
                q = stat(G, f"{sc}_c5", rate["pm"], days)
                r[sc] = f"{q['net_c']:+.1f} [{q['lo']:+.1f},{q['hi']:+.1f}] ${q['usd_day']:+.1f}"
            rows.append(r)
        if rows:
            print(
                f"== Polymarket, first trigger per market, last {late} s, cap +5c: net c/share [90% CI] $/day@5 =="
            )
            print(pd.DataFrame(rows).to_string(index=False), "\n")
    print("== Polymarket caps, tokyo and colo, last 60 s, all assets pooled by timeframe ==")
    for tf, G in F60[F60.venue == "pm"].groupby("tf"):
        for sc in ("tokyo", "colo"):
            for c in CAPS:
                print(line(f"ALL {tf}m {sc} {c}", stat(G, f"{sc}_{c}", rate["pm"], days)))
    print(
        "\n== Spread at the trigger (Polymarket, first trigger per market, last 60 s): do wins follow the ask or the mid? =="
    )
    G = F60[F60.venue == "pm"].copy()
    G["grp"] = np.where(G.tf == 15, "all 15m", np.where(G.asset == "btc", "btc 5m", "alts 5m"))
    G["sb"] = pd.cut(
        G.spread0, [-1, 300, 1000, 2000, 10000], labels=["<=3c", "3-10c", "10-20c", ">20c"]
    )
    rows = []
    for (g, b), H in G.groupby(["grp", "sb"], observed=True):
        q1, q2 = stat(H, "colo_c99", rate["pm"], days), stat(H, "tokyo_c5", rate["pm"], days)
        rows.append({"grp": g, "spread": b, "n": len(H), "win%": round(100 * (H.tok == H.win).mean(), 1),
                     "ask0": round(H.a0.mean() / 100, 1), "mid0": round((H.a0 - H.spread0 / 2).mean() / 100, 1),
                     "colo_c99": round(q1["net_c"], 1), "tokyo_c5": round(q2["net_c"], 1)})  # fmt: skip
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))
    print("\n== Polymarket BTC 5m by level X (all triggers, last 60 s, tokyo c5) ==")
    A = D[(D.venue == "pm") & (D.asset == "btc") & (D.tf == 5) & (D.t_s >= 240)]
    for X, G in A.groupby("X"):
        print(line(f"btc 5m X{X / 100:.0f}c", stat(G, "tokyo_c5", rate["pm"], days)))
    if (D.venue != "pm").any():
        print(
            f"\n== Lighter BTC/ETH 15m (fee {a.lt_fee}·p(1-p)); lt = own-book breakout, pm>lt = Polymarket signal =="
        )
        for late in (60, 180):
            F = first_per_market(D, late)
            for (v, asset), G in F[F.venue != "pm"].groupby(["venue", "asset"]):
                for sc in LT_HOLD:
                    for c in CAPS:
                        print(
                            line(
                                f"{v} {asset} last{late}s {sc} {c}",
                                stat(G, f"{sc}_{c}", rate[v], days),
                            )
                        )
            print()
        Q = F60[F60.venue == "pm>lt"]
        if len(Q):
            lag = (Q.a0 - Q.lt_a0)[Q.lt_a0 > 0] / 100
            print(f"pm>lt: Lighter ask at the Polymarket trigger is {lag.median():+.1f}c below Polymarket's (median; p25/p75 "
                  f"{lag.quantile(0.25):+.1f}/{lag.quantile(0.75):+.1f}); Lighter quote missing {np.mean(Q.lt_a0 <= 0):.0%}")  # fmt: skip


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("eval")
    p.add_argument("--rec", type=Path, required=True, help="shadow_rec output (Polymarket)")
    p.add_argument("--lt", type=Path, default=None, help="lighter_rec output (optional)")
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--days", nargs="*")
    p.add_argument("--taker-delay-ms", type=float, default=50.0)
    p.add_argument("--proc-ms", type=float, default=10.0)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--fee-rate", type=float, default=0.07)
    p.add_argument("--lt-fee", type=float, default=0.01)
    p.add_argument("--taker-delay-ms", type=float, default=50.0)
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--since", default="2000-01-01", help="first day to count (YYYY-MM-DD)")
    a = ap.parse_args()
    {"eval": cmd_eval, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
