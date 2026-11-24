"""D3 tables from out/d3/taker-*.csv.gz. usage: d3_report.py dev|holdout [--cells JSON]
Per asset x timeframe x rule: attempts/day, fill and pessimistic-fill rate, fills/day, fee, mid markouts 1/5/30 s
(gross of fee), 5 s taker exit, settle net of fee with SE (market-clustered, floored at the binomial SE at the paid
price) and 90% market-bootstrap CI, settle on the pessimistic fills, and settle minus 1.4c (the live taker slippage).
"""
import csv
import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "out" / "d3"
DEV = ["2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"]
HO = ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"]
RULES = ("F1", "F5", "F6", "LB-60", "LB-70", "LB-80")


def f(v):
    return float(v) if v not in ("", None) else np.nan


def load(asset, tf, days):
    rows = []
    for d in days:
        p = OUT / f"taker-{asset}-{tf}-{d}.csv.gz"
        if p.exists():
            rows += list(csv.DictReader(gzip.open(p, "rt")))
    return rows


def se_cl(vals, groups, price):
    v, g = np.asarray(vals, float), np.asarray(groups)
    n = len(v)
    if n < 2:
        return float("nan"), (float("nan"), float("nan"))
    m = v.mean()
    _, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv, v - m)
    se = math.sqrt((s * s).sum()) / n
    q = float(np.mean(price))
    se = max(se, 100 * math.sqrt(max(q * (1 - q), 1e-6) / n))
    ug, inv = np.unique(g, return_inverse=True)
    sv, sn = np.bincount(inv, v), np.bincount(inv)
    pick = np.random.default_rng(0).integers(0, len(ug), (1000, len(ug)))
    lo, hi = np.percentile(sv[pick].sum(1) / sn[pick].sum(1), [5, 95])
    return se, (lo, hi)


def cell(rows, ndays):
    tried = len(rows)
    fl = [r for r in rows if r["filled"] == "1"]
    st = [r for r in fl if r.get("settle_c") not in ("", None)]
    if not st:
        return dict(tried=tried, n=0)
    v = np.array([f(r["settle_c"]) for r in st])
    se, ci = se_cl(v, [r["e"] for r in st], [f(r["px"]) / 1e4 for r in st])
    ps = [f(r["settle_c"]) for r in st if r["pess"] == "1"]
    mo = {k: np.nanmean([f(r[k]) for r in fl]) if any(r[k] not in ("", None) for r in fl) else np.nan for k in ("mo1", "mo5", "mo30")}
    return dict(tried=tried, n=len(st), apd=tried / ndays, fpd=len(fl) / ndays, fill=len(fl) / tried,
                pfill=sum(r["pess"] == "1" for r in fl) / tried, fee=np.mean([f(r["fee_c"]) for r in fl]), **mo,
                exit5=np.nanmean([f(r["exit5_c"]) for r in fl]) if any(r["exit5_c"] for r in fl) else np.nan,
                settle=v.mean(), se=se, lo=ci[0], hi=ci[1], t=v.mean() / se if se > 0 else np.nan,
                pess=np.mean(ps) if ps else np.nan, npess=len(ps), live=v.mean() - 1.4,
                mde=2.5 * se)


def row(name, s):
    if not s.get("n"):
        return f"| {name} | {s.get('tried', 0)} tries, no settled fills |" + " |" * 10
    g = lambda x: f"{x:+.2f}" if x == x else ""
    return (f"| {name} | {s['apd']:.0f} | {s['fill']:.2f} / {s['pfill']:.2f} | {s['fpd']:.0f} | {s['fee']:.2f} | "
            f"{g(s['mo1'])} / {g(s['mo5'])} / {g(s['mo30'])} | {g(s['exit5'])} | **{s['settle']:+.2f}** ± {s['se']:.2f} "
            f"[{s['lo']:+.2f}, {s['hi']:+.2f}] (t {s['t']:+.1f}) | {g(s['pess'])} | {s['live']:+.2f} | {s['mde']:.1f} |")


HDR = ("| market, rule | tries/day | fill / pess fill | fills/day | fee ¢ | mo 1 s / 5 s / 30 s ¢ | exit 5 s ¢ | "
       "settle ¢ ± SE [90% CI] (t) | settle, pess fills ¢ | settle − 1.4¢ | 2.5 SE ¢ |\n|---|---|---|---|---|---|---|---|---|---|---|")


def main():
    which = sys.argv[1]
    days = DEV if which == "dev" else HO
    print(HDR)
    res = {}
    for asset in ("btc", "eth", "sol", "xrp", "doge"):
        for tf in (5, 15):
            rows = load(asset, tf, days)
            nd = len({1 for _ in [0]}) and len([d for d in days if (OUT / f"taker-{asset}-{tf}-{d}.csv.gz").exists()])
            for rule in RULES:
                s = cell([r for r in rows if r["rule"] == rule], max(nd, 1))
                res[(asset, tf, rule)] = s
                print(row(f"{asset} {tf}m {rule}", s))
    ts = [s["t"] for s in res.values() if s.get("n", 0) >= 10 and s["t"] == s["t"]]
    print(f"\nCells with >= 10 settled fills: {len(ts)}; t >= 2: {sum(t >= 2 for t in ts)}, t <= -2: {sum(t <= -2 for t in ts)}; "
          f"chance ≈ {0.0228 * len(ts):.1f} each sign.")
    if which == "dev":
        cand = sorted(((s["t"], k) for k, s in res.items() if s.get("n", 0) >= 30 and s["t"] >= 2.5 and s["pess"] > 0
                       and s["settle"] >= 1.0), reverse=True)[:3]
        print("Freeze candidates (settle >= +1c, t >= 2.5 with the floored SE, pessimistic fills > 0, >= 30 fills; max 3): " +
              ("; ".join(f"{k[0]} {k[1]}m {k[2]} (t {t:.2f})" for t, k in cand) or "none"))


if __name__ == "__main__":
    main()
