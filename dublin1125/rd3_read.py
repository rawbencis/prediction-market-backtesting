"""R-D3 read (registered test, rule in ~/dublin-retest/frozen/R-D3.md). RUN ONLY ON OR AFTER THE READ DATE (>= 2026-11-29),
ONCE. Written 2026-11-24 without opening any ~/rd3-res file.

Rule (R-D3.md, verbatim): "Pass at >= 14 settled days: >= 1,000 fills, net >= +1.0c/share vs settlement, day-bootstrap 90% CI
lower bound > 0, tokyo clearly worse than dublin. Kill: CI upper bound < +1.0c after 14 days."
Primary = src "fut" (Binance USD-M), scenario "dublin", filled rows, pnl_c (c/share, net of the 0.07 fee, vs Gamma winner).

  ~/shadow-venv/bin/python dublin1125/rd3_read.py --res ~/rd3-res --from 2026-11-15 --to 2026-11-28

Days are counted as "settled days" only if their CSV exists AND has >= 1 dublin/fut row (a decision, filled or not) and
the recorder covered >= 23 h of the day (--min-hours; checked from ~/shadow-rec/DAY/pm-HH.bin files present). Days that
fail are listed, not counted. The recorders were off from 2026-11-24 10:59 UTC; 11-24 is partial (11 h) and is listed.
"tokyo clearly worse" is printed for the reader to judge (mean and CI of tokyo next to dublin); this script does not
decide it.
"""

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=Path, default=Path.home() / "rd3-res")
    ap.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    ap.add_argument("--from", dest="d0", default="2026-11-15")
    ap.add_argument("--to", dest="d1", required=True)
    ap.add_argument("--min-hours", type=int, default=23)
    ap.add_argument("--draws", type=int, default=10000)
    a = ap.parse_args()

    days, skipped, frames = [], [], {}
    d = dt.date.fromisoformat(a.d0)
    while d <= dt.date.fromisoformat(a.d1):
        s = d.isoformat()
        f = a.res / f"rd3-{s}.csv"
        hours = len(list((a.rec / s).glob("pm-*.bin"))) if (a.rec / s).is_dir() else 0
        if not f.exists():
            skipped.append((s, "no csv"))
        elif hours < a.min_hours:
            skipped.append((s, f"recorder covered {hours} h"))
        else:
            x = pd.read_csv(f)
            if not ((x["src"] == "fut") & (x["scn"] == "dublin")).any():
                skipped.append((s, "no dublin/fut decisions"))
            else:
                days.append(s)
                frames[s] = x
        d += dt.timedelta(days=1)

    print(f"settled days counted: {len(days)} ({days[0] if days else '-'} .. {days[-1] if days else '-'})")
    for s, why in skipped:
        print(f"  not counted: {s} ({why})")
    rng = np.random.default_rng(20261129)

    def stats(scn):
        per = []
        for s in days:
            x = frames[s]
            k = (x["src"] == "fut") & (x["scn"] == scn) & (x["filled"] == 1) & x["pnl_c"].notna()
            v = pd.to_numeric(x.loc[k, "pnl_c"], errors="coerce").dropna().to_numpy()
            per.append(v)
        n = sum(len(v) for v in per)
        if n == 0:
            return n, float("nan"), (float("nan"), float("nan"))
        mean = float(np.concatenate(per).mean())
        sums = np.array([v.sum() for v in per])
        cnts = np.array([len(v) for v in per])
        idx = rng.integers(0, len(per), size=(a.draws, len(per)))
        bs = sums[idx].sum(1) / np.maximum(cnts[idx].sum(1), 1)
        return n, mean, (float(np.percentile(bs, 5)), float(np.percentile(bs, 95)))

    res = {scn: stats(scn) for scn in ("dublin", "tokyo", "colo")}
    for scn, (n, m, (lo, hi)) in res.items():
        print(f"{scn:7s} fills={n:6d} net={m:+.3f} c/share  90% day-bootstrap CI [{lo:+.3f}, {hi:+.3f}]")
    n, m, (lo, hi) = res["dublin"]
    enough = len(days) >= 14
    if enough and n >= 1000 and m >= 1.0 and lo > 0:
        verdict = "PASS on the numeric bars; the reader must still confirm 'tokyo clearly worse than dublin' from the lines above"
    elif enough and hi < 1.0:
        verdict = "KILL (CI upper bound < +1.0c after >= 14 settled days)"
    elif not enough:
        verdict = f"NOT READABLE YET: {len(days)} settled days < 14"
    else:
        verdict = "NEITHER pass nor kill at this read (rule has no third outcome: report as not passed)"
    print("VERDICT:", verdict)


if __name__ == "__main__":
    main()
