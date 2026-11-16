"""Family 2: do other wallets' taker prints follow specific wallets' prints (copy-bots / slower followers)?

For every taker print i (wallet L, direction d) we count other wallets' same-direction prints in
  after  = (t+A0, t+A1]  and the mirror control  before = [t-A1, t-A0)   (same market),
and for the opposite direction too. Excess = after - before (same dir) - (after - before) (opposite dir),
which removes generic activity bursts. Wallets with large, consistent excess are "leaders".
Pairs (L, F): F's same-direction prints after L vs before L.
Also: can a leader be recognised from the print alone (size pattern)?
Usage: python f2_leaders.py DAY [DAY...]  (dev days only) -> prints tables
"""

import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

import pmlat

A0, A1 = 200, 3000


def main():
    days = sys.argv[1:]
    D = pd.concat(
        [pd.read_csv(pmlat.OUT / f"ev-btc5-{d}.csv.gz", usecols=["e", "tp", "dir", "usd", "sz", "px", "w", "tleft"],
                     dtype={"w": str}).assign(day=d) for d in days]
    )
    D["w"] = D.w.fillna("")
    print("prints", len(D), "with wallet", (D.w != "").mean().round(3))
    per = defaultdict(lambda: np.zeros(5))  # n, after_same, before_same, after_opp, before_opp
    pair_a, pair_b = Counter(), Counter()
    for (day, e), g in D.groupby(["day", "e"], sort=False):
        g = g.sort_values("tp")
        t, d, w = g.tp.to_numpy(), g.dir.to_numpy(), g.w.to_numpy()
        for i in range(len(g)):
            if not w[i]:
                continue
            a_lo, a_hi = np.searchsorted(t, t[i] + A0, "right"), np.searchsorted(t, t[i] + A1, "right")
            b_lo, b_hi = np.searchsorted(t, t[i] - A1, "left"), np.searchsorted(t, t[i] - A0, "left")
            sa = (d[a_lo:a_hi] == d[i]) & (w[a_lo:a_hi] != w[i])
            sb = (d[b_lo:b_hi] == d[i]) & (w[b_lo:b_hi] != w[i])
            oa = (d[a_lo:a_hi] != d[i]) & (w[a_lo:a_hi] != w[i])
            ob = (d[b_lo:b_hi] != d[i]) & (w[b_lo:b_hi] != w[i])
            per[w[i]] += (1, sa.sum(), sb.sum(), oa.sum(), ob.sum())
            for f in set(w[a_lo:a_hi][sa]):
                if f:
                    pair_a[(w[i], f)] += 1
            for f in set(w[b_lo:b_hi][sb]):
                if f:
                    pair_b[(w[i], f)] += 1
    P = pd.DataFrame([(k, *v) for k, v in per.items()], columns=["w", "n", "as", "bs", "ao", "bo"])
    P["excess"] = ((P["as"] - P["bs"]) - (P["ao"] - P["bo"])) / P.n
    P["ratio"] = (P["as"] + 1) / (P["bs"] + 1)
    tot = P[["n", "as", "bs", "ao", "bo"]].sum()
    print(f"all prints: after-same {tot['as'] / tot.n:.3f} before-same {tot.bs / tot.n:.3f} "
          f"after-opp {tot.ao / tot.n:.3f} before-opp {tot.bo / tot.n:.3f} per print")
    big = P[P.n >= 100].sort_values("excess", ascending=False)
    pd.set_option("display.width", 200)
    print("\nwallets with >= 100 taker prints:", len(big))
    print(big.head(15).to_string(index=False))
    print(big.tail(5).to_string(index=False))
    Q = pd.DataFrame([(a, f, c, pair_b.get((a, f), 0)) for (a, f), c in pair_a.items()], columns=["L", "F", "after", "before"])
    Q["d"] = Q.after - Q.before
    print("\ntop leader->follower pairs (F prints same dir 0.2-3 s after L, minus 0.2-3 s before):")
    print(Q.sort_values("d", ascending=False).head(15).to_string(index=False))
    P.to_csv(pmlat.OUT / f"f2-wallets-{'_'.join(days)}.csv", index=False)
    Q[Q.after >= 5].to_csv(pmlat.OUT / f"f2-pairs-{'_'.join(days)}.csv", index=False)


if __name__ == "__main__":
    main()
