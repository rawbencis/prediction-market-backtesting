"""F6 grid (maker A by tpost x K x C x model; taker C/F by Wd x X). Usage: python f6_grid.py SCEN DAY [DAY..]"""
import sys

import pandas as pd

import pmlat


def main():
    scn, days = sys.argv[1], sys.argv[2:]
    D = pd.concat([pd.read_csv(pmlat.OUT / f"f6-{scn}-{d}.csv.gz").assign(day=d) for d in days], ignore_index=True)
    nd = len(days)
    rows = []
    A = D[D.kind == "A"]
    for (tp, K, C, model), G in A.groupby(["tpost", "K", "C", "model"]):
        F = G[G.filled > 0].reset_index(drop=True)
        for ex in ("settle_c", "mo1000", "mo30000"):
            v = F[ex].dropna()
            if len(v) < 10:
                continue
            lm = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].e.to_numpy())
            ld = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].day.to_numpy())
            rows.append(dict(kind="A", cfg=f"tpost{tp} K{K} C{C}", model=model, exit=ex, orders=len(G), fills=len(v),
                             per_day=len(v) / nd, fill_rate=len(F) / len(G), net=v.mean(), lo_m=lm[0], hi_m=lm[1], lo_d=ld[0], hi_d=ld[1]))
    for (kind, Wd, X), G in D[D.kind.isin(["C", "F"])].groupby(["kind", "Wd", "X"]):
        F = G[G.filled > 0].reset_index(drop=True)
        for ex in ("settle_c", "tk30000"):
            v = F[ex].dropna()
            if len(v) < 10:
                continue
            lm = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].e.to_numpy())
            ld = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].day.to_numpy())
            rows.append(dict(kind=kind, cfg=f"Wd{Wd} X{X}", model="taker", exit=ex, orders=len(G), fills=len(v), per_day=len(v) / nd,
                             fill_rate=len(F) / len(G), net=v.mean(), lo_m=lm[0], hi_m=lm[1], lo_d=ld[0], hi_d=ld[1]))
    R = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print("configs:", len(R))
    for k in ("A", "C", "F"):
        X = R[R.kind == k].sort_values("net", ascending=False)
        print(f"\n== {k} ==")
        print(X.head(12).round(2).to_string(index=False))
    R.to_csv(pmlat.OUT / f"f6grid-{scn}-{'_'.join(days)}.csv", index=False)


if __name__ == "__main__":
    main()
