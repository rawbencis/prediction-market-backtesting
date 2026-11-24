"""D3: the live `bid` join-maker on each alt 5m (alt_bid.py output, PMLAT_STRICT=1, `live`), maker rebate 0.
Per asset and half: orders/day, filled shares/day, 1 s / 5 s markouts, settle c/share +- SE (clustered by market,
floored at the binomial SE at the fill price), 90% market-bootstrap CI, c per $ of notional."""
import csv, gzip, math, sys
from pathlib import Path
import numpy as np
OUT = Path(__file__).resolve().parent.parent / "out" / "alts"
H = {"dev": ["2026-11-%02d" % d for d in range(14, 19)], "holdout": ["2026-11-%02d" % d for d in range(19, 24)]}
print("| asset | half | days | orders/day | filled sh/day | mo 1 s ¢ | mo 5 s ¢ | settle ¢/sh ± SE [90% CI] (t) | ¢ per $ | $ notional/day |\n|---|---|---|---|---|---|---|---|---|---|")
for a in ("eth", "sol", "xrp", "doge"):
    for half, days in H.items():
        rows, nd = [], 0
        for d in days:
            p = OUT / f"ab-{a}5-{d}.csv.gz"
            if p.exists():
                nd += 1
                rows += list(csv.DictReader(gzip.open(p, "rt")))
        fl = [r for r in rows if r["filled"] not in ("", "0", "0.0") and r["settle_c"] not in ("",)]
        if not fl:
            print(f"| {a} | {half} | {nd} | no fills |"); continue
        sz = np.array([float(r["filled"]) for r in fl]); st = np.array([float(r["settle_c"]) for r in fl])
        px = np.array([float(r["px"]) / 1e4 for r in fl]); e = np.array([r["e"] for r in fl])
        mo1 = np.array([float(r["mo1000"]) if r["mo1000"] else np.nan for r in fl]); mo5 = np.array([float(r["mo5000"]) if r["mo5000"] else np.nan for r in fl])
        m = (st * sz).sum() / sz.sum()
        _, inv = np.unique(e, return_inverse=True)
        s = np.bincount(inv, (st - m) * sz); se = math.sqrt((s * s).sum()) / sz.sum()
        q = (px * sz).sum() / sz.sum(); se = max(se, 100 * math.sqrt(q * (1 - q) / len(fl)))
        gp, gs = np.bincount(inv, st * sz), np.bincount(inv, sz)
        pick = np.random.default_rng(0).integers(0, len(gp), (1000, len(gp)))
        lo, hi = np.percentile(gp[pick].sum(1) / gs[pick].sum(1), [5, 95])
        ok1, ok5 = np.isfinite(mo1), np.isfinite(mo5)
        notional = (px * sz).sum()
        print(f"| {a} | {half} | {nd} | {len(rows) / nd:,.0f} | {sz.sum() / nd:,.0f} | {(mo1[ok1] * sz[ok1]).sum() / sz[ok1].sum():+.2f} | "
              f"{(mo5[ok5] * sz[ok5]).sum() / sz[ok5].sum():+.2f} | {m:+.2f} ± {se:.2f} [{lo:+.2f}, {hi:+.2f}] (t {m / se:+.1f}) | "
              f"{(st * sz).sum() / notional:+.2f} | {notional / nd:,.0f} |")
