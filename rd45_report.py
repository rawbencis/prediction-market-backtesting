"""R-D4 / R-D5 report (F7i-B / F7i-A, frozen 2026-11-16 in frozen/F7i.json): day-bootstrap pass/kill over every evaluated day.

Reads out/pmlat/f7i-dublin-{day}[-strict].csv.gz for every day >= SINCE (written nightly by rd45_eval.sh).
Per pick: days, fills, net c/share (share-weighted), day-bootstrap 90% CI, market-bootstrap CI, mean 1 s markout, consecutive days with markout <= 0.
Pass/kill exactly as in the overnight report (2026-11-17, section 4). Usage: python rd45_report.py [--since 2026-11-17] [--strict]
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat

PICKS = {
    "R-D4 (F7i-B)": dict(rules="out+alt+ttl", own=None, pass_net=1.0, pass_mo=0.4),
    "R-D5 (F7i-A)": dict(rules="out+opp+dep+alt+ttl", own=0.6, pass_net=1.5, pass_mo=0.8),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-17")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    sfx = "-strict" if a.strict else ""
    fs = sorted(p for p in Path(pmlat.OUT).glob(f"f7i-dublin-????-??-??{sfx}.csv.gz")
                if p.name[11:21] >= a.since and (a.strict or not p.name.endswith("-strict.csv.gz")) and "-s25" not in p.name)
    if not fs:
        print("no evaluated days since", a.since)
        return
    D = pd.concat([pd.read_csv(f).assign(day=f.name[11:21]) for f in fs], ignore_index=True)
    D = D[(D.thr == 50) & (D.model == "pess")]
    D["own"] = np.where(D.tok == 0, D.imb, -D.imb)
    days = sorted(D.day.unique())
    print(f"days {len(days)}: {days[0]}..{days[-1]}  fill model: {'strict' if a.strict else 'dsim'}")
    for name, p in PICKS.items():
        C = D[D.rules == p["rules"]]
        if p["own"] is not None:
            C = C[C.own > p["own"]]
        F = C[C.filled > 0].reset_index(drop=True)
        w, v = F.filled.to_numpy(float), F.settle_c.to_numpy(float)
        ok = ~np.isnan(v)
        net = (v[ok] * w[ok]).sum() / w[ok].sum()
        lo_d, hi_d = pmlat.boot_ci(v[ok] * w[ok], F.day.to_numpy()[ok], weights=w[ok])
        lo_m, hi_m = pmlat.boot_ci(v[ok] * w[ok], F.e.to_numpy()[ok], weights=w[ok])
        mo_day = F.groupby("day").mo1000.mean().reindex(days)
        run, worst = 0, 0
        for x in mo_day.fillna(0):
            run = run + 1 if x <= 0 else 0
            worst = max(worst, run)
        mo = F.mo1000.mean()
        n_d = len(days)
        passed = n_d >= 14 and len(F) >= 500 and net >= p["pass_net"] and lo_d > 0 and mo >= p["pass_mo"]
        killed = (n_d >= 14 and hi_d < 1.0) or worst >= 3
        print(f"{name}: fills {len(F)} ({len(F) / n_d:.0f}/day) net {net:+.2f}c/share  CI day [{lo_d:+.2f},{hi_d:+.2f}] "
              f"mkt [{lo_m:+.2f},{hi_m:+.2f}]  mo1s {mo:+.2f}c  max run of days with mo1s<=0: {worst}  "
              f"$/day@5 {(v[ok] * w[ok]).sum() / 100 / n_d:+.0f}  -> {'PASS' if passed else 'KILL' if killed else 'running'}")
        print("   per day net c/share:", " ".join(f"{d[5:]}:{x:+.1f}" for d, x in
              F[ok].assign(vw=v[ok] * w[ok], w=w[ok]).groupby("day").apply(lambda G: G.vw.sum() / G.w.sum()).items()))


if __name__ == "__main__":
    main()
