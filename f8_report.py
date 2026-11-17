"""F8: rank the dev grid (rule frozen in frozen/F8_rule.json) or evaluate given configs on given days.
  python f8_report.py rank            -> dev table for all configs, eligible top list, writes frozen/F8.json (only if absent)
  python f8_report.py eval DAY [..]   -> frozen/F8.json configs (+ F7i-B-equivalent anchor) on those days, dsim and strict
"""
import json
import sys

import numpy as np
import pandas as pd

import f8_grid
import lat_stats
import pmlat

DEV = ("2026-11-14", "2026-11-15")
ANCHOR = "new|1|alt+out|5000|off"  # = F7i-B rules (out+alt+ttl 5 s, improve +1) with ackc


def load(days, sfx=""):
    F = pd.concat([pd.read_csv(pmlat.OUT / f"f8-live_lean-{d}{sfx}.csv.gz").assign(day=d) for d in days], ignore_index=True)
    O = pd.concat([pd.read_csv(pmlat.OUT / f"f8-live_lean-{d}{sfx}-orders.csv") for d in days]).groupby("cfg").sum()
    return F, O


def main():
    if sys.argv[1] == "rank":
        F, O = load(DEV)
        rows = []
        for cfg in f8_grid.ALL:
            G = F[F.cfg == cfg]
            s = lat_stats.stats(G, int(O.orders.get(cfg, 0)), len(DEV))
            fast = O.n_fast.get(cfg, 0) / max(O.orders.get(cfg, 1), 1)
            rows.append(dict(cfg=cfg, fast=fast, **s))
        R = pd.DataFrame(rows)
        R["eligible"] = (R.fpd >= 200) & (R.mo > 0)
        R = R.sort_values("lo", ascending=False)
        print(f"configs {len(R)}; eligible (fills/day>=200 & mo1s>0) {int(R.eligible.sum())}; "
              f"settle CI lo>0 {int((R.lo > 0).sum())}; mo1s>0 {int((R.mo > 0).sum())}; both {int(((R.lo > 0) & (R.mo > 0)).sum())}")
        print("| cfg (entry, offset, pulls, ttl, cap) | eligible | fast-post share " + lat_stats.HDR)
        print("|---|---|---:" + lat_stats.SEP)
        for r in R.itertuples():
            print(f"| `{r.cfg}` | {'yes' if r.eligible else ''} | {r.fast:.2f} " + r.cells)
        E = R[R.eligible]
        top = list(E.cfg[:2]) if len(E) >= 2 else list(R.cfg[:2])
        p = pmlat.Path(__file__).resolve().parent / "frozen" / "F8.json"
        if not p.exists():
            p.write_text(json.dumps(dict(picks=top, n_eligible=int(len(E)), rule="frozen/F8_rule.json",
                                         dev={c: R.set_index("cfg").loc[c, ["net", "lo", "hi", "mo", "fpd"]].to_dict() for c in top}), indent=1))
        print("PICKS", top, "(frozen file:", p, ")")
    else:
        days = sys.argv[2:]
        picks = json.load(open(pmlat.Path(__file__).resolve().parent / "frozen" / "F8.json"))["picks"]
        print("| cfg | split | fill model " + lat_stats.HDR)
        print("|---|---|---" + lat_stats.SEP)
        for sfx, fm in (("", "dsim"), ("-strict", "strict")):
            try:
                F, O = load(days, sfx + "-ho")
            except FileNotFoundError:
                print(f"| | | {fm} not run |")
                continue
            for cfg in picks + [ANCHOR]:
                G = F[F.cfg == cfg]
                s = lat_stats.stats(G, int(O.orders.get(cfg, 0)), len(days))
                print(f"| `{cfg}`{' (anchor)' if cfg == ANCHOR and cfg not in picks else ''} | {','.join(d[5:] for d in days)} | {fm} " + s["cells"])


if __name__ == "__main__":
    main()
