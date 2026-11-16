"""F3 grid: rules x spread filter x price band x time-left x exit, per queue model. Net c/share per filled order
(first-fill share-weighted: filled shares). Usage: python f3_grid.py SCEN DAY [DAY..]"""

import sys

import pandas as pd

import pmlat

COLS = ["e", "spr", "tleft0", "tok", "px", "rules", "model", "filled", "mo1000", "mo5000", "mo30000", "settle_c", "exit_c", "exit_mk"]


def load(scn, days):
    return pd.concat([pd.read_csv(pmlat.OUT / f"f3-btc5-{scn}-{d}.csv.gz", usecols=COLS).assign(day=d) for d in days],
                     ignore_index=True)


def grid(D, ndays):
    rows = []
    for (rules, model), G0 in D.groupby(["rules", "model"]):
        for spr in ("1t", "ge2t", "any"):
            for band in ("20-80", "any"):
                for tl in ("gt60", "any"):
                    G = G0
                    if spr == "1t":
                        G = G[G.spr == 100]
                    elif spr == "ge2t":
                        G = G[G.spr >= 200]
                    if band == "20-80":
                        G = G[G.px.between(2000, 8000)]
                    if tl == "gt60":
                        G = G[G.tleft0 > 60000]
                    F = G[G.filled > 0]
                    for ex in ("settle_c", "exit_c", "mo1000", "mo30000"):
                        v = F[ex].dropna()
                        if len(v) < 20:
                            continue
                        lo_m, hi_m = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].e.to_numpy())
                        lo_d, hi_d = pmlat.boot_ci(v.to_numpy(), F.loc[v.index].day.to_numpy())
                        rows.append(dict(rules=rules, model=model, spr=spr, band=band, tl=tl, exit=ex, orders=len(G),
                                         fills=len(v), per_day=len(v) / ndays, net=v.mean(), lo_m=lo_m, hi_m=hi_m,
                                         lo_d=lo_d, hi_d=hi_d))
    return pd.DataFrame(rows)


def main():
    scn, days = sys.argv[1], sys.argv[2:]
    R = grid(load(scn, days), len(days))
    pd.set_option("display.width", 250)
    print("configs:", len(R), "(per model:", len(R[R.model == "pess"]), ")")
    for model in ("pess", "prop", "front"):
        X = R[(R.model == model) & R.exit.isin(["settle_c", "exit_c"])].sort_values("net", ascending=False)
        print(f"\n== {model}: top 8 (settle/exit) ==")
        print(X.head(8).round(2).to_string(index=False))
        print(f"median net by exit ({model}):", R[R.model == model].groupby("exit").net.median().round(2).to_dict())
    R.to_csv(pmlat.OUT / f"f3grid-{scn}-{'_'.join(days)}.csv", index=False)


if __name__ == "__main__":
    main()
