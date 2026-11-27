"""PF1 clock check: per day, Polymarket (Dublin) receive - exchange ms on BTC 15m book rows and trades, and Lighter
(Tokyo export) receive - exchange ms. Usage: python -I clock_check.py DAY [DAY ...]"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from engine import CACHE, LIMPORT, load_pm_hour  # noqa: E402


def pct(v):
    v = np.asarray(v)
    return [round(float(np.percentile(v, q)), 1) for q in (10, 50, 90)] if len(v) else None


def main():
    print("day | PM source | PM book r-x p10/p50/p90 ms | PM trade r-x p50 | PM drop rows | Lighter recv-exch p10/p50/p90 ms | Lighter rows | Lighter unknown rows")
    for day in sys.argv[1:]:
        bk, tr, drops, src = [], [], 0, "-"
        for hh in range(24):
            c = load_pm_hour(day, hh)
            if c is None:
                continue
            src = c["source"]
            g = c["x"] > 0
            d = c["r"][g] - c["x"][g]
            bk.append(d[::20])
            drops += int((~g).sum())
            if len(c["tr"]):
                tr.append(c["tr"][:, 0] - c["tr"][:, 1])
        fp = LIMPORT / f"{day}.csv.gz"
        lrow = lunk = 0
        lp = None
        if fp.exists():
            df = pd.read_csv(fp, usecols=["t_exch_ms", "t_recv_ms", "bid1", "ask1"])
            lrow = len(df)
            lunk = int(((df.bid1 <= 0) & (df.ask1 <= 0)).sum())
            lp = pct(df.t_recv_ms - df.t_exch_ms)
        b = np.concatenate(bk) if bk else []
        t = np.concatenate(tr) if tr else []
        print(f"{day} | {src} | {pct(b)} | {pct(t)[1] if len(t) else None} | {drops} | {lp} | {lrow} | {lunk}", flush=True)


if __name__ == "__main__":
    main()
