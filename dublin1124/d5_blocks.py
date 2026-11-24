"""D5 conditioning: the live `bid` join-maker (60 s, no pull; p2_join.py cfg bid|60000|none, live, strict) on BTC 5m and
15m, all recorded days. Per fill: UTC hour, weekday vs weekend, and r = sigma_60s / sigma_30min of 1 s Binance spot log
returns as received here at the fill (the D2 definition; a level, not a trigger). Blocks: 1 s markout (mo1000) and
settle, mean +- SE clustered by market, in each half of the sample (11-14..18 and 11-19..23).
usage: d5_blocks.py TF -> markdown
"""
import csv
import gzip
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "out" / "p2"
REC = Path.home() / "shadow-rec"
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
DAYS = ["2026-11-%02d" % d for d in range(14, 24)]
H1 = set(DAYS[:5])


def r_at(day, times):
    """r = sd(1 s log returns, last 60 s) / sd(last 1800 s) at each time (ms, local receipt clock ~ exchange + 9 ms)."""
    prev = str(np.datetime64(day) - np.timedelta64(1, "D"))
    parts = []
    for d, hs in ((prev, [23]), (day, range(24))):
        for h in hs:
            f = REC / d / f"bn-{h:02d}.bin"
            if f.exists():
                x = np.fromfile(f, dtype=BN_DT)
                parts.append(x[(x["src"] == 0) & (x["a"] == 0)])
    bn = np.concatenate(parts)
    bn = bn[np.argsort(bn["l"], kind="stable")]
    bl, bp = bn["l"] // 1000, bn["p"]
    out = []
    for t in times:
        secs = np.arange(t - 1_800_000, t + 1, 1000)
        i = np.searchsorted(bl, secs, "right") - 1
        if i[0] < 0:
            out.append(np.nan)
            continue
        lr = np.diff(np.log(bp[i]))
        sl, ss = lr.std(ddof=1), lr[-60:].std(ddof=1)
        out.append(ss / sl if sl > 0 else np.nan)
    return np.array(out)


def ms(v, g):
    v, g = np.asarray(v, float), np.asarray(g)
    ok = np.isfinite(v)
    v, g = v[ok], g[ok]
    if len(v) < 2:
        return np.nan, np.nan, len(v)
    m = v.mean()
    _, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv, v - m)
    return m, math.sqrt((s * s).sum()) / len(v), len(v)


def main():
    tf = int(sys.argv[1])
    rows = []
    for d in DAYS:
        p = OUT / f"p2-btc{tf}-live-{d}-strict-d5.csv.gz"
        if not p.exists():
            continue
        fl = [r for r in csv.DictReader(gzip.open(p, "rt")) if r["tfill"] not in ("", None) and float(r["filled"] or 0) > 0]
        if not fl:
            continue
        rr = r_at(d, [int(float(r["tfill"])) for r in fl])
        for r, x in zip(fl, rr):
            t = datetime.fromtimestamp(int(float(r["tfill"])) / 1000, timezone.utc)
            rows.append(dict(day=d, e=r["e"], hour=t.hour, wkend=t.weekday() >= 5, r=x,
                             mo1=float(r["mo1000"]) if r["mo1000"] not in ("", None) else np.nan,
                             st=float(r["settle_c"]) if r["settle_c"] not in ("", None) else np.nan))
    print(f"BTC {tf}m `bid` 60 s no pull: {len(rows):,} fills on {len({r['day'] for r in rows})} days "
          f"(half 1 = 11-14..18, half 2 = 11-19..23).\n")
    print("| block | fills h1 / h2 | mo 1 s ¢ h1 ± SE | mo 1 s ¢ h2 ± SE | settle ¢ h1 ± SE | settle ¢ h2 ± SE | mo1 >= 0 in both |")
    print("|---|---|---|---|---|---|---|")
    blocks = [(f"hour {h:02d}", lambda r, h=h: r["hour"] == h) for h in range(24)]
    blocks += [("weekday", lambda r: not r["wkend"]), ("weekend", lambda r: r["wkend"])]
    blocks += [("r < 0.7", lambda r: r["r"] < 0.7), ("r 0.7-1.4", lambda r: 0.7 <= r["r"] <= 1.4), ("r > 1.4", lambda r: r["r"] > 1.4)]
    blocks += [("all", lambda r: True)]
    n_pass = 0
    for name, fn in blocks:
        a = [r for r in rows if r["day"] in H1 and fn(r)]
        b = [r for r in rows if r["day"] not in H1 and fn(r)]
        m1 = ms([r["mo1"] for r in a], [r["e"] for r in a]); m2 = ms([r["mo1"] for r in b], [r["e"] for r in b])
        s1 = ms([r["st"] for r in a], [r["e"] for r in a]); s2 = ms([r["st"] for r in b], [r["e"] for r in b])
        ok = m1[0] >= 0 and m2[0] >= 0
        n_pass += ok and name != "all"
        f = lambda x: f"{x[0]:+.2f} ± {x[1]:.2f}" if x[0] == x[0] else ""
        print(f"| {name} | {len(a):,} / {len(b):,} | {f(m1)} | {f(m2)} | {f(s1)} | {f(s2)} | {'**yes**' if ok else 'no'} |")
    print(f"\nBlocks with a 1 s markout >= 0 in both halves: {n_pass} of {len(blocks) - 1}.")


if __name__ == "__main__":
    main()
