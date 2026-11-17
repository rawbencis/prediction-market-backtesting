"""Part 1 table: frozen F7i-A / F7i-B under dublin, live_lean, live_ack (each without / with the ack-constrained cancel),
dsim and strict fill models, dev (11-14..15) and holdout (11-16). Reads -v2 files (2026-11-17 rerun) where present."""
import numpy as np
import pandas as pd

import lat_stats
import pmlat

SPLITS = {"dev": ("2026-11-14", "2026-11-15"), "holdout": ("2026-11-16",)}
SCNS = ("dublin", "dublin+ackc", "live_lean", "live_lean+ackc", "live_ack", "live_ack+ackc")
PICKS = {"F7i-A": lambda D: D[(D.rules == "out+opp+dep+alt+ttl") & (D.own > 0.6)],
         "F7i-B": lambda D: D[D.rules == "out+alt+ttl"]}


def path(scn, d, sfx):
    base, ack = scn.split("+")[0], "-ackc" if scn.endswith("+ackc") else ""
    p = pmlat.OUT / f"f7i-{base}-{d}{sfx}{ack}-v2.csv.gz"
    return p if p.exists() else pmlat.OUT / f"f7i-{base}-{d}{sfx}{ack}.csv.gz"


print("| pick | split | fill model | scenario " + lat_stats.HDR)
print("|---|---|---|---" + lat_stats.SEP)
for pick, sel in PICKS.items():
    for split, days in SPLITS.items():
        for sfx, fm in (("", "dsim"), ("-strict", "strict")):
            for scn in SCNS:
                fs = [path(scn, d, sfx) for d in days]
                if not all(f.exists() for f in fs):
                    print(f"| {pick} | {split} | {fm} | {scn} | not run |")
                    continue
                D = pd.concat([pd.read_csv(f).assign(day=d) for f, d in zip(fs, days)], ignore_index=True)
                D = D[(D.thr == 50) & (D.model == "pess")]
                D["own"] = np.where(D.tok == 0, D.imb, -D.imb)
                C = sel(D)
                s = lat_stats.stats(C[C.filled > 0], len(C), len(days))
                print(f"| {pick} | {split} | {fm} | {scn} " + s["cells"])
