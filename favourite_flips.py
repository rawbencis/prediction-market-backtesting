"""Favourite-flip study on BTC 5m up/down markets (uses the same_token_wf2 prep cache).

A side is the favourite at threshold X when its mid >= X. Episode k is the k-th time the
favourite side changes (k=1: first side to reach X; k=2: the other side reaches X; ...).

Entries are MAKER only (fee 0, rebates not counted): at episode k, after LAT, rest a buy at the
chosen side's best bid; it fills only if that side's ask later trades BELOW the limit (with
>= SIZE shown) within the order lifetime. If the order is still resting when the side's book
goes dark into the close and that side loses, it counts as filled (a collapsing token's bid
gets hit). Held to settlement.

extract: one row per (market, X, episode): favourite side, crossing time, and for the new
         favourite and the underdog: limit, placement time, first trade-through time.
stats:   flips-per-market distribution, P(settles with the last favourite),
         P(another flip | k), maker fill rate, P(win | filled) and EV, by k / side / lifetime.
wf:      walk-forward over (X, k, side, crossing-time bucket, lifetime) with a frozen holdout.
"""

import argparse
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import same_token_wf2 as wf

XS = (550, 600, 650, 700, 750, 800, 850, 900)  # milli
TB = (0, 60, 120, 180, 240, 300)  # crossing-time bucket edges, s
TTLS = (10, 30, 60, 300)  # maker order lifetime, s (300 = until close)
SIDE_COLS = ("lim", "place_s", "fill_s", "dark_s")
COLS = ["day", "epoch", "x", "k", "t_s", "side", "n_flips", "winner"] + [
    f"{w}_{c}" for w in ("fav", "dog") for c in SIDE_COLS
]


def episodes(m, xs=XS, lat=wf.LAT_US, gap=wf.GAP_US, age=wf.AGE_US, size=wf.SIZE):
    """-> {x: [(k, t_us, side, fav_info, dog_info)]}; info = (limit_milli, place_us, fill_us,
    dark_us) with -1 for not applicable (no placement before close / no fill / not dark)."""
    t = m["t"]
    n = len(t)
    out = {x: [] for x in xs}
    if n == 0:
        return out
    gs = (m["bid"] >= 0) & ((t[:, None] - m["seen"]) <= age)
    gapi = np.diff(t, prepend=t[0]) > gap
    gapi[0] = t[0] > gap
    use = gs.all(1) & ~gapi
    ui = np.flatnonzero(use)
    msum = m["bid"] + m["ask"]  # 2*mid, milli
    side_use = [np.flatnonzero(gs[:, k] & ~gapi) for k in (0, 1)]
    tail_bad = wf.MKT_US - t[-1] > gap
    dark = [
        int(t[u[-1]])
        if len(u) and not tail_bad and wf.MKT_US - t[u[-1]] <= age
        else (int(t[u[-1]]) if len(u) else 0)
        for u in side_use
    ]
    is_dark = [tail_bad or not len(u) or wf.MKT_US - t[u[-1]] > age for u in side_use]

    def maker(s, T):
        u = side_use[s]
        p = np.searchsorted(u, np.searchsorted(t, T + lat))
        if p >= len(u):
            return (-1, -1, -1, dark[s])
        j0 = u[p]
        lim = int(m["bid"][j0, s])
        later = u[p + 1 :]
        hit = later[(m["ask"][later, s] < lim) & (m["asz"][later, s] >= size)]
        return (lim, int(t[j0]), int(t[hit[0]]) if len(hit) else -1, dark[s] if is_dark[s] else -1)

    for x in xs:
        up, dn = msum[ui, 0] >= 2 * x, msum[ui, 1] >= 2 * x
        st = np.where(up & ~dn, 0, np.where(dn & ~up, 1, -1))
        idx, seq = ui[st >= 0], st[st >= 0]
        if not len(idx):
            continue
        start = np.r_[True, seq[1:] != seq[:-1]]
        for k, (i, sd) in enumerate(zip(idx[start], seq[start]), 1):
            T = int(t[i])
            out[x].append((k, T, int(sd), maker(sd, T), maker(1 - sd, T)))
    return out


def _day(args):
    ddir, sett = args
    rows = []
    for e, m in wf.markets(ddir):
        w = sett.get(e)
        if w is None:
            continue
        for x, eps in episodes(m).items():
            if not eps:
                rows.append([ddir.name, e, x, 0, np.nan, -1, 0, w] + [-1] * 8)
            for k, T, sd, fav, dog in eps:
                info = [
                    v if i == 0 else (v / 1e6 if v >= 0 else -1)
                    for side in (fav, dog)
                    for i, v in enumerate(side)
                ]
                rows.append([ddir.name, e, x, k, T / 1e6, sd, len(eps), w] + info)
    return ddir.name, rows


def cmd_extract(a):
    sett = wf.settlements(a.cache)
    if not sett:
        sys.exit("no settlements.csv in cache; run same_token_wf2.py settle first")
    rows = []
    with Pool(a.workers) as pool:
        for d, r in pool.imap_unordered(_day, [(d, sett) for d in wf.done_days(a.cache)]):
            rows += r
            print(d, len(r), flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLS).sort_values(["epoch", "x", "k"]).to_csv(a.out, index=False)
    print("wrote", a.out, len(rows))


# ---------------------------------------------------------------- analysis


def load(path):
    """Adds, per side (fav/dog) and lifetime L: filled_L and pnl_L (per share, 0 if unfilled)."""
    df = pd.read_csv(path)
    df["fav_win"] = (df["side"] == df["winner"]).astype(float)
    for w in ("fav", "dog"):
        win = df["fav_win"] if w == "fav" else 1 - df["fav_win"]
        lim, place = df[f"{w}_lim"] / 1000, df[f"{w}_place_s"]
        fill, dark = df[f"{w}_fill_s"], df[f"{w}_dark_s"]
        placed = df[f"{w}_lim"] > 0
        for L in TTLS:
            end = place + L
            hit = placed & (fill >= 0) & (fill <= end)
            # still resting when the side's book went dark into the close, and the side lost
            dark_fill = placed & ~hit & (dark >= 0) & (end > dark) & (win == 0)
            f = hit | dark_fill
            df[f"{w}_filled_{L}"] = np.where(placed, f.astype(float), np.nan)
            df[f"{w}_pnl_{L}"] = np.where(placed, np.where(f, win - lim, 0.0), np.nan)
    df["kk"] = df["k"].clip(upper=4)
    df["tb"] = pd.cut(df["t_s"], TB, right=False, labels=False)
    return df


def boot_mean(df, col, n=1000):
    """Mean per trade with a 90% CI resampling whole days."""
    rng = np.random.default_rng(0)
    g = df.dropna(subset=[col]).groupby("day")[col].agg(["sum", "count"])
    if g["count"].sum() == 0:
        return np.nan, np.nan, np.nan
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    b = [s[i].sum() / max(1, c[i].sum()) for i in rng.integers(0, len(g), (n, len(g)))]
    return s.sum() / c.sum(), np.percentile(b, 5), np.percentile(b, 95)


def cmd_stats(a):
    df = load(a.episodes)
    mk = df.groupby(["x", "epoch"]).agg(n=("n_flips", "first"), w=("winner", "first"))
    last = df[df.k > 0].sort_values("k").groupby(["x", "epoch"]).last()
    print(
        f"markets: {df.epoch.nunique()}  days: {df.day.nunique()}  entry: maker at bid, "
        "trade-through fill, fee 0, rebates not counted\n"
    )
    print("== flips per market (share) and P(settles with last favourite)")
    print(f"{'X':>5} {'0':>6} {'1':>6} {'2':>6} {'3':>6} {'4+':>6} {'last_fav_wins':>14}")
    for x, g in mk.groupby(level=0):
        n = g["n"].clip(upper=4).value_counts(normalize=True).reindex(range(5), fill_value=0)
        lw = (last.loc[x, "side"] == last.loc[x, "winner"]).mean()
        print(f"{x / 1000:5.2f} " + " ".join(f"{v:6.3f}" for v in n) + f" {lw:14.3f}")

    rows = []
    for (x, kk), g in df[df.k > 0].groupby(["x", "kk"]):
        first = g.groupby("epoch").first()
        more = (first["n_flips"] > first["k"]).mean()
        for w in ("fav", "dog"):
            for L in TTLS:
                ok = g[g[f"{w}_lim"] > 0]
                f = ok[f"{w}_filled_{L}"] == 1
                win = ok["fav_win"] if w == "fav" else 1 - ok["fav_win"]
                ev, lo, hi = boot_mean(ok[f], f"{w}_pnl_{L}")
                rows.append(
                    {
                        "x": x / 1000,
                        "k": f"{kk}+" if kk == 4 else kk,
                        "side": w,
                        "ttl_s": L,
                        "episodes": len(first),
                        "p_more_flips": round(more, 3),
                        "avg_limit": round(ok[f"{w}_lim"].mean() / 1000, 3),
                        "fill_rate": round(f.mean(), 3),
                        "win_if_unfilled": round(win[~f].mean(), 3),
                        "win_if_filled": round(win[f].mean(), 3),
                        "fills": int(f.sum()),
                        "ev_per_fill": round(ev, 4),
                        "ci90": [round(lo, 4), round(hi, 4)],
                        "ev_per_episode": round(ok[f"{w}_pnl_{L}"].sum() / max(len(ok), 1), 4),
                    }
                )
    t = pd.DataFrame(rows)
    t.to_csv(a.episodes.with_name("maker_ev_by_k.csv"), index=False)
    print(
        "\n== at the k-th flip: P(another flip); maker buy at that side's bid: fill rate, "
        "win rate filled vs unfilled, EV per share"
    )
    print(t.to_string(index=False))

    rows = []
    for (x, kk, tb), g in df[(df.k > 0) & df.tb.notna()].groupby(["x", "kk", "tb"]):
        for w in ("fav", "dog"):
            for L in TTLS:
                ok = g[(g[f"{w}_lim"] > 0) & (g[f"{w}_filled_{L}"] == 1)]
                ev, lo, hi = boot_mean(ok, f"{w}_pnl_{L}")
                rows.append(
                    {
                        "x": x / 1000,
                        "k": kk,
                        "t_bucket": f"{TB[int(tb)]}-{TB[int(tb) + 1]}s",
                        "side": w,
                        "ttl_s": L,
                        "fills": len(ok),
                        "ev_per_fill": round(ev, 4),
                        "ci90_lo": round(lo, 4),
                        "ci90_hi": round(hi, 4),
                    }
                )
    tt = pd.DataFrame(rows)
    tt.to_csv(a.episodes.with_name("maker_ev_by_k_time.csv"), index=False)
    sig = tt[(tt.ci90_lo > 0) & (tt.fills >= 200)]
    print(
        f"\n== cells (X, k, time, side, ttl) with >=200 fills and 90% CI above zero: {len(sig)}"
        f" of {len(tt)} (all periods, not selection-adjusted)"
    )
    if len(sig):
        print(sig.sort_values("ev_per_fill", ascending=False).head(20).to_string(index=False))


def cmd_wf(a):
    df = load(a.episodes)
    df = df[df.k > 0]
    days = sorted(d for d, n in df.groupby("day").epoch.nunique().items() if n >= a.min_markets)
    df = df[df.day.isin(days)]
    long = []
    for w in ("fav", "dog"):
        for L in TTLS:
            g = df[["day", "epoch", "x", "kk", "tb", "k", f"{w}_pnl_{L}", f"{w}_filled_{L}"]]
            g = g.rename(columns={f"{w}_pnl_{L}": "pnl", f"{w}_filled_{L}": "filled"})
            g = g.dropna(subset=["pnl"])
            for anytime in (False, True):
                h = g.assign(tbk=5 if anytime else g["tb"], side=w, ttl=L).dropna(subset=["tbk"])
                long.append(h)
    long = pd.concat(long)
    long["tbk"] = long["tbk"].astype(int)
    long = long.sort_values("k").drop_duplicates(["epoch", "x", "kk", "side", "tbk", "ttl"])
    cands = sorted(long.groupby(["x", "kk", "side", "tbk", "ttl"]).groups)
    cid = {c: i for i, c in enumerate(cands)}
    long["c"] = [cid[c] for c in zip(long.x, long.kk, long.side, long.tbk, long.ttl)]
    P = np.zeros((len(days), len(cands)))
    N = np.zeros_like(P)
    S = np.zeros_like(P)
    for d, g in long.groupby("day"):
        di = days.index(d)
        mat = (
            g.pivot_table(index="epoch", columns="c", values="pnl", aggfunc="sum")
            .reindex(columns=range(len(cands)))
            .fillna(0)
            .sort_index()
            .to_numpy()
        )
        eq = np.cumsum(mat, 0)
        dd = (np.maximum.accumulate(np.vstack([np.zeros((1, mat.shape[1])), eq]), 0)[1:] - eq).max(
            0
        )
        P[di], S[di] = eq[-1], eq[-1] - 0.5 * dd
        N[di] = np.bincount(g["c"], weights=g["filled"], minlength=len(cands))
    nd, w = len(days), a.train_days + a.validation_days
    if nd < w + a.holdout_days + 1:
        sys.exit(f"{nd} usable days; need > {w + a.holdout_days}")
    h0 = nd - a.holdout_days
    picks, oos, frozen = [], [], None
    for i in range(w, nd):
        if i <= h0:
            sel = wf.choose(
                S,
                N,
                list(range(i - w, i - a.validation_days)),
                list(range(i - a.validation_days, i)),
                a.min_entries,
                a.top_k,
            )
            frozen = sel if i == h0 else frozen
        else:
            sel = frozen
        picks.append(None if sel is None else cands[sel])
        oos.append(
            (
                days[i],
                "holdout" if i >= h0 else "dev_oos",
                picks[-1],
                0.0 if sel is None else P[i, sel],
                0 if sel is None else int(N[i, sel]),
            )
        )

    def summ(phase, col=None):
        r = [o for o in oos if o[1] == phase]
        v = np.array([o[3] for o in r]) if col is None else P[[days.index(o[0]) for o in r], col]
        n = (
            sum(o[4] for o in r)
            if col is None
            else int(N[[days.index(o[0]) for o in r], col].sum())
        )
        b = np.random.default_rng(0).choice(v, (2000, len(v))).mean(1)
        return {
            "days": len(v),
            "total": round(float(v.sum()), 3),
            "trades": n,
            "per_trade": round(float(v.sum() / max(n, 1)), 4),
            "ci90_mean_day": [
                round(float(np.percentile(b, 5)), 3),
                round(float(np.percentile(b, 95)), 3),
            ],
            "cash_days": sum(o[2] is None for o in r) if col is None else 0,
        }

    hold = list(range(h0, nd))
    out = {
        "entry": "maker at bid, trade-through fill, fee 0",
        "holdout_start": days[h0],
        "candidates": len(cands),
        "frozen": cands[frozen] if frozen is not None else None,
        "dev_oos": summ("dev_oos"),
        "holdout": summ("holdout"),
        "holdout_grid_share_positive": round(float((P[hold].sum(0) > 0).mean()), 3),
        "selection_counts": pd.Series([str(p) for p in picks]).value_counts().to_dict(),
        "fixed_no_selection": {},
    }
    for c in [
        (x, kk, s, 5, 60) for x in (650, 700, 750) for kk in (1, 2, 3) for s in ("fav", "dog")
    ]:
        if c in cid:
            out["fixed_no_selection"][str(c)] = {
                "dev": summ("dev_oos", cid[c]),
                "holdout": summ("holdout", cid[c]),
            }
    (a.episodes.with_name("flips_wf.json")).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("extract")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    for name in ("stats", "wf"):
        p = sp.add_parser(name)
        p.add_argument("--episodes", type=Path, required=True)
        if name == "wf":
            p.add_argument("--train-days", type=int, default=14)
            p.add_argument("--validation-days", type=int, default=7)
            p.add_argument("--holdout-days", type=int, default=10)
            p.add_argument("--min-entries", type=int, default=30)
            p.add_argument("--top-k", type=int, default=20)
            p.add_argument("--min-markets", type=int, default=250)
    a = ap.parse_args()
    {"extract": cmd_extract, "stats": cmd_stats, "wf": cmd_wf}[a.cmd](a)


if __name__ == "__main__":
    main()
