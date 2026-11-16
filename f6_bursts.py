"""Family 6: timing of taker flow inside BTC 5m windows (prints from the event table).
Per second-of-window: prints, taker $ volume, net direction (UP-buy $ - UP-sell $), mean |mid move| over the next 5 s,
and whether flow in that second is predictable from the previous window state (direction of the mid since open).
Usage: python f6_bursts.py DAY [DAY..]"""

import sys

import numpy as np
import pandas as pd

import pmlat


def main():
    days = sys.argv[1:]
    D = pd.concat([pd.read_csv(pmlat.OUT / f"ev-btc5-{d}.csv.gz", usecols=["e", "tp", "dir", "usd", "px", "tleft", "mv5000", "w"],
                               dtype={"w": str}).assign(day=d) for d in days])
    D["sec"] = ((D.tp - D.e * 1000) // 1000).astype(int)
    D = D[(D.sec >= -45) & (D.sec < 300)]
    nm = D.groupby("day").e.nunique().sum()
    G = D.groupby("sec").agg(n=("usd", "size"), usd=("usd", "sum"), big=("usd", lambda s: (s >= 100).sum()))
    G["n_per_mkt"] = G.n / nm
    G["usd_per_mkt"] = G.usd / nm
    pd.set_option("display.width", 200)
    sl = G.copy()
    sl["blk"] = (sl.index // 15) * 15
    print("per 15-s block of the window (sec from open; negative = pre-open):")
    print(sl.groupby("blk")[["n_per_mkt", "usd_per_mkt", "big"]].sum().round(2).T.to_string())
    top = G.sort_values("usd_per_mkt", ascending=False).head(12)
    print("\nbusiest seconds by $/market:\n", top.round(2).to_string())
    # within-second timing: ms of the print stamp inside the second (fixed-schedule bots cluster)
    D["ms"] = D.tp % 1000
    h = np.histogram(D.ms, bins=20, range=(0, 1000))[0]
    print("\nms-in-second histogram (20 bins of 50 ms), all prints:", h.tolist())
    hb = np.histogram(D[D.usd >= 100].ms, bins=20, range=(0, 1000))[0]
    print("same, prints >= $100:", hb.tolist())
    # top wallets by $ and their timing
    W = D[D.w.notna()].groupby("w").agg(n=("usd", "size"), usd=("usd", "sum"), sec_med=("sec", "median"),
                                         ms_iqr=("ms", lambda s: s.quantile(0.75) - s.quantile(0.25)))
    print("\ntop takers by $:\n", W.sort_values("usd", ascending=False).head(12).round(1).to_string())


if __name__ == "__main__":
    main()
