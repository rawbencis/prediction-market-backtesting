"""PF1 holdout evaluation of the frozen pick (frozen/PF1_pick.json) under frozen/PF1_holdout_rule.json.

PASS requires every plan criterion (c/share per attempted share >= +2.0 incl. unwinds, 90% lower bound of $/day > 0,
>= 100 attempts, >= 5 of 7 days positive) to hold on BOTH
  (1) all attempts of the pick, as-is, and
  (2) clean books only: attempts whose Polymarket book in force at the entry match is <= 2000 ms old (match exchange
      time - exchange time of the last Dublin book row) - other attempts are removed; windows, days and the bootstrap
      (window, 4000, seed 0) are unchanged, so a window with only stale attempts counts as $0.
The book-age diagnostic (distribution, attempts and $ by age > 1 / 2 / 5 s) is printed with the verdict.
Usage: python -I holdout_eval.py --days D1..D7 [--res ~/pf1-cache/res] [--out verdict.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agg import load, table  # noqa: E402
from stale_diag import PICK, ages  # noqa: E402

MAX_AGE_MS = 2000.0
KEY = ["day", "e", "t"]


def criteria(row, nd):
    c = dict(cps=float(row.cps), lo90=float(row.lo90), attempts=int(row.attempts), days_pos=int(row.days_pos), days=nd,
             usd_day=float(row.usd_day), hi90=float(row.hi90))
    c["pass_cps"] = c["cps"] >= 2.0
    c["pass_lo90"] = c["lo90"] > 0
    c["pass_attempts"] = c["attempts"] >= 100
    c["pass_days"] = c["days_pos"] >= 5
    c["pass"] = all(c[k] for k in ("pass_cps", "pass_lo90", "pass_attempts", "pass_days"))
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--res", type=Path, default=Path.home() / "pf1-cache" / "res")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cell = dict(kv.split("=") for kv in PICK.split(","))
    A, W = load(a.res, a.days)
    for k, v in cell.items():
        A = A[A[k].astype(str) == v]
    G = ages(a.days, a.res, cell)
    A = A.merge(G[KEY + ["age_ms"]], on=KEY, how="left", validate="one_to_one")
    assert A.age_ms.notna().all(), "attempt without a book age"
    nd = len(a.days)
    res = dict(days=a.days, pick=cell, max_age_ms=MAX_AGE_MS)
    res["as_is"] = criteria(table(A, W, a.days).iloc[0], nd)
    C = A[A.age_ms <= MAX_AGE_MS]
    res["clean"] = criteria(table(C, W, a.days).iloc[0], nd) if len(C) else dict(attempts=0, **{"pass": False})
    res["PASS"] = bool(res["as_is"]["pass"] and res["clean"]["pass"])
    q = A.age_ms.quantile([0.5, 0.9, 0.99])
    res["book_age"] = dict(p50_ms=float(q[0.5]), p90_ms=float(q[0.9]), p99_ms=float(q[0.99]),
                           **{f"over_{s}s": dict(attempts=int((A.age_ms > s * 1000).sum()), usd=round(float(A.pnl[A.age_ms > s * 1000].sum()), 2))
                              for s in (1, 2, 5)}, total_usd=round(float(A.pnl.sum()), 2))
    print(json.dumps(res, indent=1))
    if a.out:
        a.out.write_text(json.dumps(res, indent=1) + "\n")


if __name__ == "__main__":
    main()
