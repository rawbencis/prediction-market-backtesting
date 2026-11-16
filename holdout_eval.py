"""Evaluate ONLY the frozen picks (frozen/*.json) on given days, per scenario. No grid search here.
Usage: python holdout_eval.py DAY [DAY..]   (scenarios dublin and tokyo where the sim files exist)
Prints, per frozen pick: fills, fills/day, net c/share, 90% CI by market and by day (the wider is the one to judge),
$/day at 5 shares, and the 1 s markout where defined."""

import sys

import numpy as np
import pandas as pd

import f1_grid
import pmlat


def ci_line(name, v, groups_m, groups_d, ndays, extra=""):
    v = np.asarray(v, float)
    if len(v) < 2:
        print(f"{name:<34} n {len(v)}")
        return
    lm = pmlat.boot_ci(v, groups_m)
    ld = pmlat.boot_ci(v, groups_d) if len(set(groups_d)) > 1 else (np.nan, np.nan)
    print(f"{name:<34} n {len(v):>5} /day {len(v) / ndays:7.1f} net {v.mean():+6.2f}c CI mkt [{lm[0]:+.2f},{lm[1]:+.2f}] "
          f"day [{ld[0]:+.2f},{ld[1]:+.2f}] $/day@5 {5 * v.sum() / 100 / ndays:+7.2f} {extra}")


def f1(days, scn):
    try:
        D = f1_grid.load(scn, days)
    except FileNotFoundError:
        return
    G = f1_grid.dedupe(D[(D.side == "follow") & (D.usd >= 100) & (D.filled == 1) & (D.swept == 1)]).reset_index(drop=True)
    v = G.settle.dropna()
    ci_line(f"F1 follow>=100 swept settle [{scn}]", 100 * v, G.loc[v.index].e, G.loc[v.index].day, len(days))


def f3i(days, scn, pfx="f3i"):
    fs = [pmlat.OUT / f"{pfx}-btc5-{scn}-{d}.csv.gz" for d in days]
    if not all(f.exists() for f in fs):
        return
    D = pd.concat([pd.read_csv(f).assign(day=d) for f, d in zip(fs, days)], ignore_index=True)
    D["own"] = np.where(D.tok == 0, D.imb, -D.imb)
    C = D[(D.rules == "out+opp+dep+ttl") & (D.model == "pess") & (D.own > 0.6)]
    F = C[C.filled > 0].reset_index(drop=True)
    v = F.settle_c.dropna()
    ci_line(f"F3i improve own>0.6 settle [{scn}]", v, F.loc[v.index].e, F.loc[v.index].day, len(days),
            f"orders {len(C)} fill {len(F) / max(len(C), 1):.2f} mo1s {F.mo1000.mean():+.2f} mo5s {F.mo5000.mean():+.2f}")
    m = F.mo1000.dropna()
    ci_line(f"F3i markout 1 s [{scn}]", m, F.loc[m.index].e, F.loc[m.index].day, len(days))


def f4(days, scn):
    fs = [pmlat.OUT / f"f4-{scn}-{d}.csv.gz" for d in days]
    if not all(f.exists() for f in fs):
        return
    D = pd.concat([pd.read_csv(f).assign(day=d) for f, d in zip(fs, days)], ignore_index=True)
    G = D[D.usd >= 50].drop_duplicates(["day", "e15"]).reset_index(drop=True)
    v = G.settle.dropna()
    ci_line(f"F4 15m after 5m>=50 settle [{scn}]", v, G.loc[v.index].e15, G.loc[v.index].day, len(days))


def f6(days, scn):
    fs = [pmlat.OUT / f"f6-{scn}-{d}.csv.gz" for d in days]
    if not all(f.exists() for f in fs):
        return
    D = pd.concat([pd.read_csv(f).assign(day=d) for f, d in zip(fs, days)], ignore_index=True)
    G = D[(D.kind == "A") & (D.tpost == 239.0) & (D.K == 0) & (D.C == 0.5) & (D.model == "pess")]
    F = G[G.filled > 0].reset_index(drop=True)
    v = F.settle_c.dropna()
    ci_line(f"F6 maker sell fav 239.0 settle [{scn}]", v, F.loc[v.index].e, F.loc[v.index].day, len(days),
            f"orders {len(G)} fill {len(F) / max(len(G), 1):.2f}")


def main():
    days = sys.argv[1:]
    print("days", days)
    for scn in ("dublin", "tokyo"):
        f1(days, scn)
        f3i(days, scn)
        f4(days, scn)
        f6(days, scn)


if __name__ == "__main__":
    main()
