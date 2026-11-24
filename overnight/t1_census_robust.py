"""T1 census-fee rerun: robustness rows (descriptive), fee-regime and fee-source splits. Reads out/t1/census/*.pkl."""
import pickle
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "out" / "t1" / "census"
rows = []
for p in sorted(OUT.glob("*.pkl")):
    if "2026-11-13" <= p.stem <= "2026-11-23":
        rows += [dict(w, day=p.stem) for w in pickle.load(p.open("rb"))]


def ci(per_w, days, seed=0):
    x = np.asarray(per_w, float)
    rng = np.random.default_rng(seed)
    t = x[rng.integers(0, len(x), (4000, len(x)))].sum(1) / days
    return np.percentile(t, 5), np.percentile(t, 95)


def cell(ws, d1, d2, filt=lambda f: True, val=lambda f: f["pnl"] * f["qty"], qty=lambda f: f["qty"]):
    per_w, sh, n = [], 0.0, 0
    for w in ws:
        s = 0.0
        for f in w["fills"]:
            if (f["d1"], f["d2"]) == (d1, d2) and f["hedged"] and filt(f):
                s += val(f)
                sh += qty(f)
                n += 1
        per_w.append(s)
    days = len(ws) / 96
    lo, hi = ci(per_w, days)
    return f"{n} fills, {100 * sum(per_w) / max(sh, 1e-9):+.2f}c, ${sum(per_w) / days:+.0f}/day [{lo:+.0f}, {hi:+.0f}]"


for nm in ("btc", "eth"):
    ws = [w for w in rows if w["asset"] == nm]
    print(f"\n{nm.upper()} ({len(ws)} windows)")
    for d in ((500, 500), (1000, 1000)):
        fs = [f for w in ws for f in w["fills"] if (f["d1"], f["d2"]) == d and f["hedged"]]
        print(f"  {d}: base {cell(ws, *d)}")
        print(f"     Lighter quote >= 5 sh 100 ms after match: {np.mean([f['pess'] for f in fs]):.3f}; PM hedge 100 ms after: {np.mean([f['pess_h'] for f in fs]):.3f}")
        print(f"     pessimistic: {cell(ws, *d, filt=lambda f: f['pess'] and f['pess_h'])}")
        print(f"     -1.4c haircut: {cell(ws, *d, val=lambda f: (f['pnl'] - 0.014) * f['qty'])}")
        print(f"     5-share clips: {cell(ws, *d, val=lambda f: f['pnl'] * 5, qty=lambda f: 5)}")
    for lab, sel in (("pre-switch windows (fee 0.07)", lambda w: w["fee"] >= 0.069),
                     ("post-switch windows", lambda w: w["fee"] < 0.069),
                     ("fee from census", lambda w: w["fsrc"] == "census"),
                     ("fee 0.07 by extrapolation (no census, 11-13..14 21:40)", lambda w: w["fsrc"] != "census")):
        sw = [w for w in ws if sel(w)]
        if sw:
            print(f"  {lab}: {len(sw)} windows, {cell(sw, 500, 500)}")
