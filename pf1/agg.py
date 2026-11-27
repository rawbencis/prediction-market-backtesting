"""PF1 aggregation: one row per cell (threshold x L x Lighter timing x M x Lighter view lag) for a variant
(Polymarket hold, unwind mode, clip) over a set of days.

Per cell: attempts/day, Polymarket fill rate (attempts with any PM fill), Lighter fill rate (of Lighter orders sent),
pairs, unwinds (count, $), shares held to settlement, c/share per attempted share net of everything, $/day,
90% CI of $/day by window bootstrap (4000 resamples of per-window $, windows with a known outcome, zero for windows
without attempts; seed 0), days positive, share of $ from the last 2 minutes.
Usage: python -I agg.py --days D1 D2 ... [--hold 150] [--unwind sell] [--clip q5] [--res ~/pf1-cache/res] [--csv out.csv]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CELL = ["th", "L", "timing", "M", "lag"]


def load(res, days):
    A = pd.concat([pd.read_csv(res / f"attempts-{d}.csv.gz") for d in days], ignore_index=True)
    W = pd.concat([pd.read_csv(res / f"windows-{d}.csv").assign(day=d) for d in days], ignore_index=True)
    W = W[W.up_won.astype(str).isin(["True", "False"])]
    return A, W


def boot(per_win, nwin, ndays, n=4000, seed=0):
    v = np.zeros(nwin)
    v[: len(per_win)] = per_win
    rng = np.random.default_rng(seed)
    s = v[rng.integers(0, nwin, size=(n, nwin))].sum(1) / ndays
    return float(np.percentile(s, 5)), float(np.percentile(s, 95))


def table(A, W, days, hold=150, unwind="sell", clip="q5"):
    A = A[(A.hold == hold) & (A.unwind == unwind) & (A.clip == clip)]
    nd, nwin = len(days), len(W)
    rows = []
    for key, g in A.groupby(CELL):
        pw = g.groupby(["day", "e"]).pnl.sum()
        lo, hi = boot(pw.to_numpy(), nwin, nd)
        byday = g.groupby("day").pnl.sum().reindex(days, fill_value=0.0)
        usd = g.pnl.sum()
        sent = g.l_sent.astype(bool)
        rows.append(dict(zip(CELL, key)) | dict(
            attempts=len(g), att_per_day=len(g) / nd, pm_fill=float((g.f_pm > 0).mean()),
            l_fill=float((g.f_l[sent] > 0).mean()) if sent.any() else float("nan"),
            pairs=int((g.pair > 0).sum()), unwinds=int((g.unwound > 0).sum()), unwind_usd=float(g.unwind_usd.sum()),
            held_sh=float(g.held.sum()), cps=100 * usd / g.q.sum(), usd_day=usd / nd, lo90=lo, hi90=hi,
            days_pos=int((byday > 0).sum()), late_share=float(g.pnl[g.late.astype(bool)].sum() / usd) if usd else float("nan"),
            pair_cps=float(np.nansum(g.pair_cps * g.pair) / g.pair.sum()) if g.pair.sum() else float("nan")))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--res", type=Path, default=Path.home() / "pf1-cache" / "res")
    ap.add_argument("--hold", type=int, default=150)
    ap.add_argument("--unwind", default="sell")
    ap.add_argument("--clip", default="q5")
    ap.add_argument("--csv", type=Path)
    a = ap.parse_args()
    A, W = load(a.res, a.days)
    T = table(A, W, a.days, a.hold, a.unwind, a.clip)
    pd.set_option("display.width", 250, "display.max_rows", 500)
    print(T.round(3).to_string(index=False))
    if a.csv:
        T.to_csv(a.csv, index=False)


if __name__ == "__main__":
    main()
