"""15m study, Part 3a: sibling-book consistency in the last 5 minutes of each BTC 15m window.
The 15m window [E, E+900) and the 5m window [E+600, E+900) settle on the Chainlink BTC/USD 60 s TWAP at E+900. Strikes:
the 60 s TWAP at each window's start second (settlement rule checked 83/83 on 2026-11-15). If S15 < S5, UP15 pays whenever
UP5 does, so UP15 must trade >= UP5: a violation is UP5 bid - UP15 ask > 0; buying UP15 at its ask and DOWN5 at 1 - UP5 bid pays
>= 1 in every outcome. Mirror for S15 > S5. Net of two taker fees (0.07 p (1-p) each).
1-s samples of both books (pm rows) from E+600 to E+895. TWAP from ~/feeds-rec/<day>/twap60-HH.jsonl.gz (gaps -> window skipped).
Usage: PMLAT_MAXDAY=... python p3_sibling.py DAY -> out/p2/sibling-{day}.csv (one row per sample with a violation > 0) + summary
"""
import csv
import glob
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import pmlat

sys.path.insert(0, str(Path.home() / "dublin-retest" / "repo"))
from settle_check import read_gz  # noqa: E402  (tolerates truncated / concatenated gzip members)

OUT = Path(__file__).resolve().parent / "out" / "p2"


def twap_series(day):
    d0 = date.fromisoformat(day)
    xs, vs = [], []
    for d in ((d0 - timedelta(1)).isoformat(), day):
        for f in sorted(glob.glob(str(Path.home() / "feeds-rec" / d / "twap60-*.jsonl.gz"))):
            for r in read_gz(f):
                if r.get("s") == "btc" and not r.get("snap") and r.get("x"):
                    xs.append(int(r["x"]))
                    vs.append(float(r["v"]))
    o = np.argsort(xs)
    return np.array(xs, np.int64)[o], np.array(vs)[o]


def at(ser, t, tol=2000):
    i = int(np.searchsorted(ser[0], t, "right")) - 1
    return float(ser[1][i]) if i >= 0 and t - ser[0][i] <= tol else None


def fee(p):
    return 0.07 * p * (1 - p)


def quotes(m, t):
    B = m["book"]
    i = int(np.searchsorted(B["x"], t, "right")) - 1
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return None
    return int(B["b"][i]) / 1e4, int(B["k"][i]) / 1e4


def main():
    day = sys.argv[1]
    ser = twap_series(day)
    print("twap rows", len(ser[0]), flush=True)
    g5 = pmlat.hours(day, "btc", 5, with_act=False)
    cur = (None, {})
    rows, stats = [], dict(windows=0, no_strike=0, samples=0, viol=0, viol_net=0)
    for h, mk15 in pmlat.hours(day, "btc", 15, with_act=False):
        while cur[0] is None or cur[0] < h:
            try:
                cur = next(g5)
            except StopIteration:
                cur = (99, {})
        mk5 = cur[1] if cur[0] == h else {}
        for E, m15 in mk15.items():
            m5 = mk5.get(E + 600)
            if m5 is None:
                continue
            s15, s5 = at(ser, E * 1000), at(ser, (E + 600) * 1000)
            stats["windows"] += 1
            if s15 is None or s5 is None or s15 == s5:
                stats["no_strike"] += 1
                continue
            for t in range((E + 600) * 1000 + 1000, (E + 895) * 1000, 1000):
                q15, q5 = quotes(m15, t), quotes(m5, t)
                if q15 is None or q5 is None:
                    continue
                stats["samples"] += 1
                if s15 < s5:  # UP15 >= UP5: buy UP15 at its ask, buy DOWN5 at 1 - UP5 bid
                    a, bb = q15[1], q5[0]
                    v = bb - a
                    net = v - fee(a) - fee(1 - bb)
                else:  # UP5 >= UP15: buy UP5 at its ask, buy DOWN15 at 1 - UP15 bid
                    a, bb = q5[1], q15[0]
                    v = bb - a
                    net = v - fee(a) - fee(1 - bb)
                if v > 0:
                    stats["viol"] += 1
                    stats["viol_net"] += int(net > 0)
                    rows.append(dict(E=E, t=t, left_s=(E + 900) - t / 1000, s15=s15, s5=s5, up15_bid=q15[0], up15_ask=q15[1],
                                     up5_bid=q5[0], up5_ask=q5[1], viol_c=round(100 * v, 2), net_c=round(100 * net, 2)))
        print(day, h, stats, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"sibling-{day}.csv", "w") as f:
        w = csv.DictWriter(f, fieldnames=["E", "t", "left_s", "s15", "s5", "up15_bid", "up15_ask", "up5_bid", "up5_ask", "viol_c", "net_c"])
        w.writeheader()
        w.writerows(rows)
    print("SUMMARY", day, stats)


if __name__ == "__main__":
    main()
