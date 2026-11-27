"""PF1 diagnostic (descriptive, after the pick): age of the Polymarket book in force at each attempt's PM match (exchange
time of match - exchange time of the last Dublin book row), for one cell. Dublin's pmbook feed has silent gaps (no rows,
no trades, no drop marker); in a gap the simulator matches on the last row seen. Out: CSV with the attempt key + age.
Usage: python -I stale_diag.py --days D... [--res ~/pf1-cache/res] --out FILE [cell defaults = the frozen pick]"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from engine import Static  # noqa: E402
from sim import Lat, iter_windows  # noqa: E402

PICK = "th=6,L=0,timing=a,M=1,lag=120,hold=320,unwind=sell,clip=q5"  # frozen/PF1_pick.json


def ages(days, res, cell):
    """-> DataFrame (day, e, t, pnl, q, f_pm, age_ms) for the attempts of `cell` (dict of attempt-column -> str value)."""
    st = Static(str(Path(__file__).parent.parent / "reports" / "dublin" / "fees_by_window.csv"))
    out = []
    for d in days:
        A = pd.read_csv(res / f"attempts-{d}.csv.gz")
        for k, v in cell.items():
            A = A[A[k].astype(str) == v]
        lat = Lat(pm_hold=float(cell["hold"]))
        by = A.groupby("e")
        for w in iter_windows(d, st, only=set(by.groups)):
            g = by.get_group(w.e)
            for _, r in g.iterrows():
                m = r.t + lat.pm_send + lat.pm_oneway + lat.pm_hold
                i = w.pm.at(m)
                out.append(dict(day=d, e=w.e, t=r.t, pnl=r.pnl, q=r.q, f_pm=r.f_pm, age_ms=m - w.pm.tx[i] if i >= 0 else float("nan")))
        del A
    return pd.DataFrame(out, columns=["day", "e", "t", "pnl", "q", "f_pm", "age_ms"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--res", type=Path, default=Path.home() / "pf1-cache" / "res")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cell", default=PICK)
    a = ap.parse_args()
    ages(a.days, a.res, dict(kv.split("=") for kv in a.cell.split(","))).to_csv(a.out, index=False)

if __name__ == "__main__":
    main()
