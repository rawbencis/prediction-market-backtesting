"""Family 1 grid over f1_sim output (+ event features). Usage: python f1_grid.py SCEN DAY [DAY..] [--top N]
Config = side x min usd x swept filter x time-left filter x exit; one trade per market per 30 s (first event wins).
Prints net c/share, fills/day, 90% CI by market and by day."""

import sys

import numpy as np
import pandas as pd

import pmlat

EXITS = ("settle", "tk1000", "tk5000", "tk30000", "mk100", "mk300")


def load(scn, days):
    out = []
    for d in days:
        S = pd.read_csv(pmlat.OUT / f"f1sim-btc5-{scn}-{d}.csv.gz")
        E = pd.read_csv(pmlat.OUT / f"ev-btc5-{d}.csv.gz", usecols=["e", "j", "tp", "usd", "swept", "tleft", "px", "same_prev5s", "w"],
                        dtype={"w": str})
        out.append(S.merge(E, on=["e", "j"], how="left").assign(day=d))
    return pd.concat(out, ignore_index=True)


def dedupe(G, gap=30000):
    G = G.sort_values(["e", "tp"])
    keep = np.zeros(len(G), bool)
    last_e, last_t = None, -1e18
    for i, (e, t) in enumerate(zip(G.e.to_numpy(), G.tp.to_numpy())):
        if e != last_e or t - last_t >= gap:
            keep[i] = True
            last_e, last_t = e, t
    return G[keep]


def stats(F, col, ndays):
    v = F[col].dropna() * 100
    if len(v) < 2:
        return None
    G = F.loc[v.index]
    lo_m, hi_m = pmlat.boot_ci(v.to_numpy(), G.e.to_numpy())
    lo_d, hi_d = pmlat.boot_ci(v.to_numpy(), G.day.to_numpy())
    return dict(n=len(v), per_day=len(v) / ndays, net=v.mean(), lo_m=lo_m, hi_m=hi_m, lo_d=lo_d, hi_d=hi_d)


def grid(D, ndays):
    rows = []
    for side in ("follow", "fade"):
        for mu in (20, 50, 100, 250):
            for sw in ("any", "swept"):
                for tl in ("all", "gt60"):
                    G = D[(D.side == side) & (D.usd >= mu) & (D.filled == 1)]
                    if sw == "swept":
                        G = G[G.swept == 1]
                    if tl == "gt60":
                        G = G[G.tleft > 60000]
                    G = dedupe(G)
                    for ex in EXITS:
                        s = stats(G, ex, ndays)
                        if s:
                            rows.append(dict(side=side, usd=mu, sw=sw, tl=tl, exit=ex, **s))
    return pd.DataFrame(rows)


def main():
    scn, days = sys.argv[1], [a for a in sys.argv[2:] if a.startswith("2026")]
    D = load(scn, days)
    print(f"{scn} {days}: events {len(D)}, filled {D.filled.mean():.3f}")
    R = grid(D, len(days))
    pd.set_option("display.width", 220)
    print("configs:", len(R))
    print(R.sort_values("net", ascending=False).head(20).round(2).to_string(index=False))
    print(R.groupby(["side", "exit"]).net.median().round(2).unstack())
    R.to_csv(pmlat.OUT / f"f1grid-{scn}-{'_'.join(days)}.csv", index=False)


if __name__ == "__main__":
    main()
