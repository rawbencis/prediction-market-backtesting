"""F7i-A / F7i-B frozen picks across latency scenarios and fill models, dev (11-14..15) and holdout (11-16).
Reads only out/pmlat/f7i-{scn}-{day}[-strict].csv.gz for those days. Prints a markdown table."""
import numpy as np
import pandas as pd

import pmlat

SPLITS = {"dev": ("2026-11-14", "2026-11-15"), "holdout": ("2026-11-16",)}
SCNS = ("dublin", "dublin+ackc", "live_hold150", "dublin_p90", "live_ack", "live_ack+ackc", "tokyo")
PICKS = {"F7i-A": lambda D: D[(D.rules == "out+opp+dep+alt+ttl") & (D.own > 0.6)],
         "F7i-B": lambda D: D[D.rules == "out+alt+ttl"]}

print("| pick | split | fill model | scenario | orders/day | fill rate | fills/day | settle c/share | 90% CI (market) | mo1s c/share | $/day (settle) |")
print("|---|---|---|---|---:|---:|---:|---:|---|---:|---:|")
for pick, sel in PICKS.items():
    for split, days in SPLITS.items():
        for sfx, fm in (("", "dsim"), ("-strict", "strict")):
            for scn in SCNS:
                base, ack = scn.split("+")[0], "-ackc" if scn.endswith("+ackc") else ""
                fs = [pmlat.OUT / f"f7i-{base}-{d}{sfx}{ack}.csv.gz" for d in days]
                if not all(f.exists() for f in fs):
                    print(f"| {pick} | {split} | {fm} | {scn} | | | – | not run | | | |")
                    continue
                D = pd.concat([pd.read_csv(f).assign(day=d) for f, d in zip(fs, days)], ignore_index=True)
                D = D[(D.thr == 50) & (D.model == "pess")]
                D["own"] = np.where(D.tok == 0, D.imb, -D.imb)
                C = sel(D)
                F = C[C.filled > 0]
                ok = F.settle_c.notna()
                w, v = F.filled[ok].to_numpy(float), F.settle_c[ok].to_numpy(float)
                lo, hi = pmlat.boot_ci(v * w, F.e[ok].to_numpy(), weights=w)
                mo = F.mo1000.dropna()
                mo1 = (mo * F.filled[mo.index]).sum() / F.filled[mo.index].sum()
                print(f"| {pick} | {split} | {fm} | {scn} | {len(C) / len(days):,.0f} | {len(F) / max(len(C), 1):.2f} | {len(F) / len(days):,.0f} | {(v * w).sum() / w.sum():+.2f} | "
                      f"[{lo:+.2f}, {hi:+.2f}] | {mo1:+.2f} | {(v * w).sum() / 100 / len(days):+,.0f} |")
