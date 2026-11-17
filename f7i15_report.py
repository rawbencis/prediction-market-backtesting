"""Summary of the frozen F7i rules on BTC 15m (p2_f7i.py outputs): per pull source x scenario, A and B.
Markouts only where > 30 s remain after the markout time; settle with market and day CIs. Usage: python f7i15_report.py [days...]"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat

OUT = Path(__file__).resolve().parent / "out" / "p2"
DAYS = sys.argv[1:] or ["2026-11-14", "2026-11-15", "2026-11-16"]


def wm(z, c, h=None):
    s = z[z[c].notna()]
    if h is not None:
        s = s[s.tleft - h > 30000]
    return float((s[c] * s.filled).sum() / s.filled.sum()) if len(s) else np.nan


print("| pick | pull source | scenario | orders/day | fills/day | mo 1 s | 5 s | 30 s | settle ¢/sh | CI market | CI day | rebate ¢ |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for src in ("alt15", "b5a5"):
    for scn in ("live", "dublin"):
        D = []
        for d in DAYS:
            f = OUT / f"f7i15-{src}-btc15-{scn}-{d}-strict.csv.gz"
            if f.exists() and f.stat().st_size > 100:
                z = pd.read_csv(f)
                z["day"] = d
                D.append(z)
        if not D:
            continue
        D = pd.concat(D)
        D["own"] = np.where(D.tok == 0, D.imb, -D.imb)
        for lab, C in (("F7i-B", D[(D.rules == "out+alt+ttl")]), ("F7i-A", D[(D.rules == "out+opp+dep+alt+ttl") & (D.own > 0.6)])):
            F = C[C.filled > 0]
            s = F[F.settle_c.notna()]
            lo, hi = pmlat.boot_ci(s.settle_c * s.filled, s.day + s.e.astype(str), weights=s.filled)
            dlo, dhi = pmlat.boot_ci(s.settle_c * s.filled, s.day, weights=s.filled)
            reb = float((100 * 0.014 * F.px / 1e4 * (1 - F.px / 1e4) * F.filled).sum() / max(F.filled.sum(), 1e-9))
            print(f"| {lab} | {src} | {scn} | {len(C) / len(DAYS):,.0f} | {len(F) / len(DAYS):,.0f} | {wm(F, 'mo1000', 1000):+.2f} | "
                  f"{wm(F, 'mo5000', 5000):+.2f} | {wm(F, 'mo30000', 30000):+.2f} | {wm(F, 'settle_c'):+.2f} | [{lo:+.2f}, {hi:+.2f}] | "
                  f"[{dlo:+.2f}, {dhi:+.2f}] | {reb:.2f} |")
