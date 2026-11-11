"""Favourite-flip MAKER bets on Lighter BTC/ETH 15m up/down, from the shadow recordings.

Same question as favourite_flips.py (Polymarket 5m), executed on Lighter:
- Clock: a side is the favourite at threshold X when its mid >= X. Flips are read on the
  Polymarket 15m mid of the same window (deep, calibrated) or, with --clock lighter, on Lighter's.
  Episode k = k-th change of the favourite side.
- Order: PLACE_LAT after the flip, good-till-time post-only buys of the new favourite or the
  underdog on Lighter (buy Down = sell Up at 1 - q), never modified or cancelled. Price rules:
    join  = Lighter's best bid of that side, posted once for the whole lifetime
    fairH = Polymarket fair of that side minus H (1c ticks, rounded down), posted once
    rfH[sS] = an S-second order (default 10) at fair - H, re-posted at the new fair each time it
            expires, until the lifetime ends or Q shares filled (new orders only: no modify /
            cancel). `posts` counts the orders sent, for the rate limit.
  An order that would cross Lighter's ask (post-only reject) is not placed.
- Fill: only by RECORDED Lighter taker trades on the other side within the lifetime. If our price
  improved Lighter's best bid we are first in line: any taker sell at <= our price fills us. If we
  joined an existing level we are behind its queue: only a trade strictly through our price counts.
  Fill size accumulates up to Q shares.
- Result: held to settlement (Lighter outcome, else Polymarket winner), maker fee 0; plus markout
  against Polymarket's fair of that side at the fill and 5 s later (adverse selection).
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

import lighter_eval as le
import lighter_rec as lr

XS = (0.60, 0.65, 0.70, 0.75, 0.80)
# join = Lighter best bid; fairH = Polymarket fair - H c, posted once; rfH[sS] = re-posted every
# S s (default 10) at the then-current fair - H c (each order expires by itself: never modified)
RULES = (
    "join",
    "fair2",
    "fair3",
    "fair5",
    "fair7",
    "fair10",
    "fair15",
    "rf3",
    "rf5",
    "rf7",
    "rf3s5",
    "rf5s5",
    "rf3s3",
    "rf5s3",
)
# fixed before data (see the project doc): (x, k, rule, life), both sides, both assets
PREREG = {
    (0.70, 1, "fair3", 60),
    (0.70, 1, "fair3", 900),
    (0.70, 1, "rf5", 900),
    (0.70, 1, "fair5", 20),
}
LIVES = (10, 20, 30, 60, 300, 900)  # s; 900 = to the close
REFRESH_S = 10
PLACE_LAT = 250_000  # us from the flip to the order resting
Q = 10.0  # shares per order
TF = 15


def valid(b, k):
    return (b > 0) & (k > b)


def flips(t, mid, ok, x):
    """-> [(k, grid index, side 0=Up/1=Down)] for threshold x on a grid of mids."""
    st = np.where(ok & (mid >= x), 0, np.where(ok & (1 - mid >= x), 1, -1))
    idx = np.flatnonzero(st >= 0)
    if not len(idx):
        return []
    seq = st[idx]
    start = np.r_[True, seq[1:] != seq[:-1]]
    return [(k, int(i), int(s)) for k, (i, s) in enumerate(zip(idx[start], seq[start]), 1)]


def side_quotes(L, i, side):
    """Lighter best bid / ask of `side` (0 Up, 1 Down) at row i, prices in $."""
    b, k = L["b1"][i] / 1e4, L["k1"][i] / 1e4
    return (b, k) if side == 0 else (1 - k, 1 - b)


def window(L, T, P, W, e, clock="pm"):
    """One (asset, window). L/T/P sorted by local time. W: 1.0 Up won / 0.0 / nan.
    -> list of dict rows, one per (x, k, side, rule, life) order placed."""
    g = np.arange(e * 1_000_000 + 5_000_000, (e + TF * 60) * 1_000_000, le.STEP)
    end_us = (e + TF * 60) * 1_000_000
    jp = le.asof(P["l"], g) if len(P) else np.full(len(g), -1)
    jpp = np.maximum(jp, 0)
    pmid = (P["b"][jpp] + P["k"][jpp]) / 2e4 if len(P) else np.zeros(len(g))
    pok = (jp >= 0) & valid(P["b"][jpp], P["k"][jpp]) if len(P) else np.zeros(len(g), bool)
    jl = le.asof(L["l"], g) if len(L) else np.full(len(g), -1)
    jll = np.maximum(jl, 0)
    lmid = (L["b1"][jll] + L["k1"][jll]) / 2e4 if len(L) else np.zeros(len(g))
    lok = (jl >= 0) & valid(L["b1"][jll], L["k1"][jll]) if len(L) else np.zeros(len(g), bool)
    mid, ok = (pmid, pok) if clock == "pm" else (lmid, lok)
    # taker trades on the trade's own clock: time, price ($, Up terms), size, taker bought Up?
    if len(T):
        tt = np.where(T["T"] > 0, T["T"].astype(np.int64) * 1000, T["l"])
        o = np.argsort(tt, kind="stable")
        tt, tp, ts, tb = tt[o], T["p"][o] / 1e4, T["s"][o].astype(float), T["buy"][o].astype(bool)
    else:
        tt, tp, ts, tb = np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0, bool)

    def pm_fair(side, t_us):
        if not len(P):
            return np.nan
        j = le.asof(P["l"], t_us)
        if j < 0 or not valid(P["b"][j], P["k"][j]):
            return np.nan
        m = (P["b"][j] + P["k"][j]) / 2e4
        return m if side == 0 else 1 - m

    rows = []
    for x in XS:
        for k, gi, fav in flips(g, mid, ok, x):
            t0 = int(g[gi]) + PLACE_LAT
            if t0 >= end_us:
                continue
            i = le.asof(L["l"], t0) if len(L) else -1
            if i < 0 or not valid(L["b1"][i], L["k1"][i]):
                continue  # no two-sided Lighter book to post into
            for who, side in (("fav", fav), ("dog", 1 - fav)):
                fair0 = pm_fair(side, t0)
                win = np.nan if np.isnan(W) else (W if side == 0 else 1 - W)
                sells = ~tb if side == 0 else tb  # taker selling `side`
                px_side = tp if side == 0 else 1 - tp  # trade price in `side` terms

                def price(rule, tj, side=side):
                    """-> (limit, first in queue) for an order posted at tj, or None."""
                    j = le.asof(L["l"], tj)
                    if j < 0 or not valid(L["b1"][j], L["k1"][j]):
                        return None
                    bid, ask = side_quotes(L, j, side)
                    if rule == "join":
                        q = round(bid, 2)
                    else:
                        fair = pm_fair(side, tj)
                        if np.isnan(fair):
                            return None
                        h = (
                            int(rule[4:] if rule.startswith("fair") else rule[2:].split("s")[0])
                            / 100
                        )
                        q = np.floor((fair - h) * 100 + 1e-9) / 100
                    if q < 0.01 or q >= ask - 1e-9:
                        return None  # post-only would be rejected
                    return q, q > bid + 1e-9  # improving Lighter's bid puts us first in line

                for rule in RULES:
                    for life in LIVES:
                        t_end = min(t0 + life * 1_000_000, end_us)
                        if rule.startswith("rf"):
                            every = int(rule.split("s")[1]) if "s" in rule else REFRESH_S
                            step = every * 1_000_000
                        else:
                            step = t_end - t0
                        fills, left, tj, posted, first0, q0, posts = (
                            [],
                            Q,
                            t0,
                            False,
                            None,
                            np.nan,
                            0,
                        )
                        while tj < t_end and left > 1e-9:
                            pq = price(rule, tj)
                            if pq is not None:
                                q, first = pq
                                posts += 1
                                if not posted:
                                    posted, first0, q0 = True, first, q
                                te = min(tj + step, t_end)
                                thru = (px_side <= q + 1e-9) if first else (px_side < q - 1e-9)
                                for ti in np.flatnonzero(sells & thru & (tt > tj) & (tt <= te)):
                                    sz = min(float(ts[ti]), left)
                                    fills.append((int(tt[ti]), sz, q))
                                    left -= sz
                                    if left <= 1e-9:
                                        break
                            tj += step
                        if not posted:
                            continue
                        filled = sum(f[1] for f in fills)
                        rows.append(
                            {
                                "x": x,
                                "k": min(k, 3),
                                "who": who,
                                "rule": rule,
                                "life": life,
                                "q0": q0,
                                "q": sum(f[1] * f[2] for f in fills) / filled if filled else np.nan,
                                "first": first0,
                                "fair0": fair0,
                                "win": win,
                                "filled": filled,
                                "posts": posts,
                                "pnl": (
                                    sum(f[1] * (win - f[2]) for f in fills)
                                    if not np.isnan(win)
                                    else np.nan
                                )
                                if filled
                                else 0.0,
                                "mo0": sum(f[1] * (pm_fair(side, f[0]) - f[2]) for f in fills),
                                "mo5": sum(
                                    f[1] * (pm_fair(side, f[0] + 5_000_000) - f[2]) for f in fills
                                ),
                            }
                        )
    return rows


def cmd_report(a):
    LB = le.read(a.lt, "lb", le.LB_DT)
    LT = le.read(a.lt, "lt", le.LT_DT)
    LB, LT = LB[LB["tf"] == TF], LT[LT["tf"] == TF]
    _, first = np.unique(LT["id"], return_index=True)
    LT = LT[np.sort(first)]
    PM = le.read(a.pm, "pm", le.PM_DT) if a.pm else np.zeros(0, le.PM_DT)
    PM = PM[PM["tf"] == TF]
    mk, lo = {}, {}
    if (a.lt / "lighter_markets.csv").exists():
        with (a.lt / "lighter_markets.csv").open() as f:
            mk = {int(r[3]): (r[0], int(r[2])) for r in csv.reader(f) if len(r) >= 4}
    if (a.lt / "lighter_outcomes.csv").exists():
        with (a.lt / "lighter_outcomes.csv").open() as f:
            lo = {mk[int(r[0])]: r[2] for r in csv.reader(f) if len(r) >= 3 and int(r[0]) in mk}
    now_e = int(LB["l"].max() // 1_000_000) if len(LB) else 0
    keys = sorted({(int(x), int(y)) for x, y in zip(LB["a"], LB["e"]) if int(y) + TF * 60 < now_e})
    win = le.pm_winners(
        a.lt / "pm_winners.csv", [(lr.ASSETS[x], e) for x, e in keys], fetch=not a.no_gamma
    )
    tab = defaultdict(lambda: [0, 0])
    for kk, code in lo.items():
        if kk in win:
            tab[code][int(win[kk])] += 1
    up_code = {c: v[1] >= v[0] for c, v in tab.items()}
    rows = []
    for ai, e in keys:
        name = lr.ASSETS[ai]
        W = (
            float(up_code[lo[(name, e)]])
            if (name, e) in lo and lo[(name, e)] in up_code
            else win.get((name, e), np.nan)
        )
        L = np.sort(LB[(LB["a"] == ai) & (LB["e"] == e)], order="l")
        T = np.sort(LT[(LT["a"] == ai) & (LT["e"] == e)], order="l")
        P = np.sort(PM[(PM["a"] == ai) & (PM["e"] == e)], order="l")
        if a.clock == "pm" and not len(P):
            continue
        rows += [{**r, "asset": name, "e": e} for r in window(L, T, P, W, e, a.clock)]
    import pandas as pd

    df = pd.DataFrame(rows)
    nwin = len({(r["asset"], r["e"]) for r in rows})
    days = max(len({e for _, e in keys}) / 96, 1e-9)
    print(
        f"windows with a flip clock: {nwin} ({days:.2f} days of 15m windows); clock={a.clock}; order {Q:.0f} sh, "
        "maker fee 0, never modified"
    )
    if df.empty:
        print("no orders yet")
        return
    df.to_csv(a.lt / "lighter_flips_orders.csv", index=False)
    rng = np.random.default_rng(0)
    out = []
    for key, gdf in df.groupby(["asset", "x", "k", "who", "rule", "life"]):
        f = gdf[gdf.filled > 0]
        sh = f.filled.sum()
        per_win = gdf.groupby("e").pnl.sum()
        b = (
            [
                per_win.to_numpy()[ix].sum()
                for ix in rng.integers(0, len(per_win), (500, len(per_win)))
            ]
            if len(per_win)
            else [0]
        )
        out.append(
            {
                "asset": key[0],
                "x": key[1],
                "k": "3+" if key[2] == 3 else key[2],
                "who": key[3],
                "rule": key[4],
                "life": key[5],
                "orders": len(gdf),
                "posts_per_order": round(gdf.posts.mean(), 1),
                "fill_rate": round((gdf.filled > 0).mean(), 3),
                "shares": round(sh, 0),
                "avg_q": round(f.q.mean(), 3) if len(f) else np.nan,
                "fair_at_post": round(gdf.fair0.mean(), 3),
                "win_if_filled": round(f.win.mean(), 3) if len(f) else np.nan,
                "ev_sh_settle": round(f.pnl.sum() / sh, 4) if sh else np.nan,
                "ev_sh_vs_fair_fill": round(f.mo0.sum() / sh, 4) if sh else np.nan,
                "ev_sh_vs_fair_5s": round(f.mo5.sum() / sh, 4) if sh else np.nan,
                "usd_day": round(gdf.pnl.sum() / days, 2),
                "usd_day_ci90": [
                    round(np.percentile(b, 5) / days, 2),
                    round(np.percentile(b, 95) / days, 2),
                ],
            }
        )
    t = pd.DataFrame(out)
    t.to_csv(a.lt / "lighter_flips_summary.csv", index=False)
    pd.set_option("display.width", 250)
    print(
        "ev_sh_* = result per filled share: to settlement, and vs Polymarket's fair of that side "
        "at the fill / 5 s later; posts_per_order = orders sent per flip (rate limit)"
    )
    pre = t[
        [
            (x, 3 if k == "3+" else k, r, lf_) in PREREG
            for x, k, r, lf_ in zip(t.x, t.k, t.rule, t.life)
        ]
    ]
    print(
        "\n== PRE-REGISTERED (judge only these: >= 5 days, settle CI > 0, 5 s markout > 0, >= 500 shares)"
    )
    print(pre.to_string(index=False))
    if a.all:
        print("\n== all cells for --show-x / --show-lives")
        print(t[(t.life.isin(a.show_lives)) & (t.x.isin(a.show_x))].to_string(index=False))
    rf = t[t.rule.str.startswith("rf") & (t.x == 0.70) & (t.k == 1) & (t.life == 900)]
    print(
        "\n== re-post variants, X 0.70, first flip, to the close (descriptive: refresh speed vs markout)"
    )
    print(
        rf[
            [
                "asset",
                "who",
                "rule",
                "posts_per_order",
                "fill_rate",
                "shares",
                "ev_sh_settle",
                "ev_sh_vs_fair_fill",
                "ev_sh_vs_fair_5s",
                "usd_day",
            ]
        ].to_string(index=False)
    )
    pos = t[(t.shares >= a.min_shares) & (t.usd_day_ci90.apply(lambda c: c[0] > 0))]
    print(
        f"\ncells with >= {a.min_shares:.0f} filled shares and a 90% CI (by window) above 0: {len(pos)} of {len(t)} "
        "(no selection adjustment: descriptive only)"
    )
    if len(pos):
        print(pos.sort_values("usd_day", ascending=False).head(15).to_string(index=False))
    print(f"\nfull table: {a.lt / 'lighter_flips_summary.csv'} (or --all)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("report")
    p.add_argument("--lt", type=Path, required=True)
    p.add_argument("--pm", type=Path, default=None)
    p.add_argument("--clock", choices=("pm", "lighter"), default="pm")
    p.add_argument("--no-gamma", action="store_true")
    p.add_argument("--min-shares", type=float, default=200)
    p.add_argument("--show-lives", type=int, nargs="+", default=[20, 60, 900])
    p.add_argument("--show-x", type=float, nargs="+", default=[0.70])
    p.add_argument(
        "--all", action="store_true", help="also print every cell for --show-x / --show-lives"
    )
    cmd_report(ap.parse_args())


if __name__ == "__main__":
    main()
