"""T4.3 summary: polled 1h/4h/1d books (out/t4/books.jsonl.gz) vs BTC 15m from ~/shadow-rec over the same hours."""
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/ubuntu/lighter-shadow")
import lighter_eval as le  # noqa: E402

rows = []
with gzip.open("out/t4/books.jsonl.gz", "rt") as f:
    try:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except EOFError:
        pass
LEN = {"1h": 3600, "4h": 14400, "1d": 86400}
t0, t1 = min(r["t"] for r in rows), max(r["t"] for r in rows)
print(f"{len(rows)} book reads, {datetime.fromtimestamp(t0, timezone.utc):%H:%M}-{datetime.fromtimestamp(t1, timezone.utc):%H:%M} UTC")
g = defaultdict(list)
for r in rows:
    g[(r["a"], r["tf"])].append(r)
print("asset tf | time left (share of window) | polls | cadence s | mid | spread c p50 (p25-p75) | size at best bid/ask p50 | depth within 3 ticks bid/ask p50 | book changed between polls | last-trade price changed")
for (a, tf), R in sorted(g.items()):
    R.sort(key=lambda r: r["t"])
    for lo, hi in ((0.75, 1.01), (0.5, 0.75), (0.25, 0.5), (0.0, 0.25)):
        S = [(i, r) for i, r in enumerate(R) if r["bids"] and r["asks"] and lo <= (r["e"] + LEN[tf] - r["t"]) / LEN[tf] < hi]
        if len(S) < 5:
            continue
        sp = [100 * (r["asks"][0][0] - r["bids"][0][0]) for _, r in S]
        mid = np.median([(r["asks"][0][0] + r["bids"][0][0]) / 2 for _, r in S])
        tick = float(S[0][1]["tick"] or 0.01)
        sb = [r["bids"][0][1] for _, r in S]
        sa = [r["asks"][0][1] for _, r in S]
        db = [sum(s for p, s in r["bids"] if p >= r["bids"][0][0] - 2 * tick - 1e-9) for _, r in S]
        da = [sum(s for p, s in r["asks"] if p <= r["asks"][0][0] + 2 * tick + 1e-9) for _, r in S]
        ch, tr, cad = [], [], []
        for i, r in S:
            if i > 0 and R[i - 1]["e"] == r["e"]:
                ch.append(R[i - 1]["h"] != r["h"])
                tr.append(R[i - 1]["ltp"] != r["ltp"])
                cad.append(r["t"] - R[i - 1]["t"])
        q = lambda x: f"{np.percentile(x, 50):.1f} ({np.percentile(x, 25):.1f}-{np.percentile(x, 75):.1f})"  # noqa: E731
        print(f"{a} {tf} | {lo:.2f}-{min(hi, 1):.2f} | {len(S)} | {np.median(cad) if cad else np.nan:.0f} | {mid:.2f} | {q(sp)} | {np.median(sb):.0f}/{np.median(sa):.0f} | "
              f"{np.median(db):.0f}/{np.median(da):.0f} | {np.mean(ch) if ch else np.nan:.2f} | {np.mean(tr) if tr else np.nan:.2f}")

# BTC 15m from shadow-rec, same hours (top of book only)
day = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d")
h0, h1 = datetime.fromtimestamp(t0, timezone.utc).hour, datetime.fromtimestamp(t1, timezone.utc).hour
P, T = [], []
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("ud", "u1"), ("side", "u1"), ("p", "<u2"), ("s", "<f4"), ("fee", "<u2"), ("tx", "S32")])
for h in range(h0, h1 + 1):
    f = Path(f"/home/ubuntu/shadow-rec/{day}/pm-{h:02d}.bin")
    if f.exists():
        x = np.fromfile(f, dtype=le.PM_DT)
        P.append(x[(x["tf"] == 15) & (x["a"] == 0)].copy())
    f = Path(f"/home/ubuntu/shadow-rec/{day}/tr-{h:02d}.bin")
    if f.exists():
        x = np.fromfile(f, dtype=TR_DT)
        T.append(x[(x["tf"] == 15) & (x["a"] == 0)].copy())
P = np.concatenate(P)
T = np.concatenate(T)
P = P[(P["x"] / 1000 >= t0) & (P["x"] / 1000 <= t1) & (P["b"] > 0) & (P["k"] > P["b"])]
T = T[(T["x"] / 1000 >= t0) & (T["x"] / 1000 <= t1)]
left = (P["e"] + 900 - P["x"] / 1000) / 900
print(f"\nBTC 15m (shadow-rec, same hours, Up token top of book; every change recorded): {len(P)} rows, {len(np.unique(P['e']))} windows")
for lo, hi in ((0.75, 1.01), (0.5, 0.75), (0.25, 0.5), (0.0, 0.25)):
    m = (left >= lo) & (left < hi)
    sp = 100 * (P["k"][m] - P["b"][m]) / 1e4
    n_w = len(np.unique(P["e"][m]))
    secs = n_w * 900 * (hi - lo if hi <= 1 else 0.25)
    tl = (T["e"] + 900 - T["x"] / 1000) / 900
    nt = ((tl >= lo) & (tl < hi)).sum()
    print(f"btc 15m | {lo:.2f}-{min(hi, 1):.2f} | spread c p50 {np.percentile(sp, 50):.1f} ({np.percentile(sp, 25):.1f}-{np.percentile(sp, 75):.1f}) | size at best bid/ask p50 "
          f"{np.median(P['bs'][m]):.0f}/{np.median(P['ks'][m]):.0f} | top-of-book changes/s {m.sum() / secs:.1f} | prints/s {nt / secs:.2f}")
