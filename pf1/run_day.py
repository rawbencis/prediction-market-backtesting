"""PF1 per-day runner: every trigger of every (Lighter view lag, threshold) and every execution variant, one row per
attempt, plus the frozen Lighter-first 500+500 rule (V2). One UTC day per process.

Out: OUT/attempts-DAY.csv.gz, OUT/v2-DAY.csv.gz, OUT/windows-DAY.csv (windows evaluated, with outcome and fee).
Usage: python -I run_day.py DAY [--out ~/pf1-cache/res] [--no-v2] [--no-pf1]
"""
import argparse
import csv
import gzip
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine import Static  # noqa: E402
from sim import Lat, frozen_500, iter_windows, pf1_attempt, triggers  # noqa: E402

LAGS = (100, 120)  # Lighter view lag at Dublin (ms) on top of the exchange time + 2 ms
THS = (4, 5, 6)  # cents
LS = (0, 1, 2)
MS = (0, 1)
TIMINGS = ("a", "b")
HOLDS = (150, 250)  # Polymarket taker hold: 'live' scenario 150 ms; alt 250 ms
UNWINDS = ("sell", "hold")
CLIPS = ("q5", "cap200")
FIELDS = ["day", "e", "lag", "th", "L", "M", "timing", "hold", "unwind", "clip", "t", "side", "edge", "q", "f_pm", "pm_px", "l_sent",
          "f_l", "l_px", "pair", "unwound", "unwind_usd", "held", "fees", "pnl", "pair_cps", "late"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--out", type=Path, default=Path.home() / "pf1-cache" / "res")
    ap.add_argument("--census", default=str(Path(__file__).parent.parent / "reports" / "dublin" / "fees_by_window.csv"))
    ap.add_argument("--no-v2", action="store_true")
    ap.add_argument("--no-pf1", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    st = Static(a.census)
    fa = gzip.open(a.out / f"attempts-{a.day}.csv.gz", "wt", newline="") if not a.no_pf1 else None
    wa = csv.DictWriter(fa, FIELDS, extrasaction="ignore") if fa else None
    if wa:
        wa.writeheader()
    fv = gzip.open(a.out / f"v2-{a.day}.csv.gz", "wt", newline="") if not a.no_v2 else None
    wv = csv.DictWriter(fv, ["day", "e", "t", "side", "qty", "pnl_sh", "usd", "late"]) if fv else None
    if wv:
        wv.writeheader()
    fw = open(a.out / f"windows-{a.day}.csv", "w", newline="")
    ww = csv.writer(fw)
    ww.writerow(["e", "up_won", "rate", "rate_src", "pm_rows", "l_rows"])
    nw = natt = 0
    for w in iter_windows(a.day, st):
        nw += 1
        ww.writerow([w.e, w.up_won, w.rate, w.rate_src, len(w.pm.tx), len(w.lt.tx)])
        if wv:
            for r in frozen_500(w):
                wv.writerow(dict(r, day=a.day))
        if not wa or w.up_won is None:
            continue
        for lag in LAGS:
            for th in THS:
                for tr in triggers(w, None, 2.0 + lag, th / 100):
                    for clip in CLIPS:
                        q = 5.0 if clip == "q5" else float(min(max(tr.lsz, 5.0), tr.psz, 200.0) // 1)
                        q = max(q, 5.0)
                        for hold in HOLDS:
                            lat = Lat(pm_hold=float(hold))
                            for L in LS:
                                for M in MS:
                                    for tm in TIMINGS:
                                        for uw in UNWINDS:
                                            if clip == "cap200" and (hold != 150 or uw != "sell"):
                                                continue  # capped size: primary hold and unwind only
                                            r = pf1_attempt(w, tr, L, M, tm, q, lat, uw)
                                            r.update(day=a.day, lag=lag, th=th, L=L, M=M, timing=tm, hold=hold, unwind=uw, clip=clip)
                                            wa.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})
                                            natt += 1
    for f in (fa, fv, fw):
        if f:
            f.close()
    print(a.day, "windows", nw, "attempt rows", natt, f"{time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
