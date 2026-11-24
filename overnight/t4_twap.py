"""T4 extra (descriptive): new-series BTC 1h markets (Chainlink range-TWAP settlement, 11-18..23).
S = Binance spot BTCUSDT from ~/shadow-rec bn rows (1 s last trade); K = S at the open second (USDT space; the
Chainlink-USD basis is taken as constant within the hour). Fair value for an average-price settlement:
  final avg = f*A + (1-f)*R,  R ~ N(S, S^2 sigma^2 tau / 3)  ->  z_twap = (f*A + (1-f)*S - K) / ((1-f) S sigma sqrt(tau/3))
sigma = sd of 1 s log returns over the trailing 6 h. Prices from CLOB prices-history (mid-like, see T4.2)."""
import csv
import json
from datetime import datetime, timedelta, timezone
from math import erf, sqrt
from pathlib import Path

import numpy as np

BN = np.dtype([("l", "<i8"), ("x", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
OUT = Path(__file__).resolve().parent / "out" / "t4"
NEW_FROM = 1794960000
secs, px = [], []
d = datetime(2026, 11, 17, tzinfo=timezone.utc)
while d < datetime(2026, 11, 24, 1, tzinfo=timezone.utc):
    f = Path(f"/home/ubuntu/shadow-rec/{d:%Y-%m-%d}/bn-{d:%H}.bin")
    if f.exists():
        x = np.fromfile(f, dtype=BN)
        x = x[(x["src"] == 0) & (x["a"] == 0) & (x["x"] > 1.79e12)]
        s = x["x"] // 1000
        o = np.argsort(x["x"], kind="stable")
        s, p = s[o], x["p"][o]
        last = np.r_[s[1:] != s[:-1], True]
        secs.append(s[last]); px.append(p[last])
    d += timedelta(hours=1)
secs, px = np.concatenate(secs), np.concatenate(px)
t0 = secs[0]
grid = np.arange(t0, secs[-1] + 1)
idx = np.searchsorted(secs, grid, "right") - 1
P = px[np.maximum(idx, 0)]  # close of second s = last trade with x < (s+1)*1000
lr = np.r_[0.0, np.diff(np.log(P))]
cs = np.r_[0.0, np.cumsum(P)]
c1, c2 = np.r_[0.0, np.cumsum(lr)], np.r_[0.0, np.cumsum(lr ** 2)]
phi = np.vectorize(lambda z: 0.5 * (1 + erf(z / sqrt(2))))
rows = []
for e_, tok, w, fee in csv.reader(open(OUT / "h1_markets.csv")):
    e = int(e_)
    if e < NEW_FROM or w == "" or not (OUT / "hist" / f"{e}.json").exists():
        continue
    i0 = e - 1 - t0  # close of second e-1 = price at the open
    if i0 < 21600:
        continue
    K = P[i0]
    for h in json.loads((OUT / "hist" / f"{e}.json").read_text()):
        t = int(h["t"])
        tau = e + 3600 - t
        if not (120 <= tau < 3600):
            continue
        i = t - 1 - t0
        if i >= len(P):
            continue
        S = P[i]
        A = (cs[i + 1] - cs[i0 + 1]) / max(i - i0, 1)
        n = 21600
        s1 = c1[i + 1] - c1[i - n + 1]
        v = max((c2[i + 1] - c2[i - n + 1]) / n - (s1 / n) ** 2, 1e-12)
        sig = sqrt(v)
        fr = (t - e) / 3600
        z = (fr * A + (1 - fr) * S - K) / ((1 - fr) * S * sig * sqrt(tau / 3))
        zend = np.log(S / K) / (sig * sqrt(tau))
        rows.append((e, tau / 60, z, zend, float(h["p"]), float(w)))
R = np.array(rows)
e, tl, z, ze, p, W = R.T
print(f"new-series markets {len(np.unique(e))}, samples {len(R)}, days {(e.max() - e.min()) / 86400 + 1 / 24:.1f}")
fair = phi(z)
print(f"model sanity (all samples): mean win {W.mean():.3f}, mean Phi(z_twap) {fair.mean():.3f}, mean price {p.mean():.3f}; "
      f"Brier price {np.mean((W - p) ** 2):.4f} vs model {np.mean((W - fair) ** 2):.4f} vs endpoint-z model {np.mean((W - phi(ze)) ** 2):.4f}")
o = np.lexsort((-tl, e))
e, tl, z, p, W, fair = e[o], tl[o], z[o], p[o], W[o], fair[o]
print("buy the favourite (by z_twap) at the first |z| >= k in each minutes-left band; one per market; edge vs price (mid-like)")
for lo, hi in ((50, 60), (30, 50), (10, 30), (2, 10)):
    for k in (0.3, 1.0):
        m = np.flatnonzero((tl >= lo) & (tl < hi) & (np.abs(z) >= k))
        _, f_ = np.unique(e[m], return_index=True)
        m = m[f_]
        if len(m) < 10:
            continue
        q = np.where(z[m] > 0, p[m], 1 - p[m]); ws = np.where(z[m] > 0, W[m], 1 - W[m]); fv = np.where(z[m] > 0, fair[m], 1 - fair[m])
        ed = ws - q
        print(f"  [{lo},{hi}) |z|>={k}: markets {len(m)}, price {q.mean():.3f}, model {fv.mean():.3f}, win {ws.mean():.3f}, edge_mid {100 * ed.mean():+.2f} ± {100 * ed.std(ddof=1) / np.sqrt(len(m)):.2f}c")
