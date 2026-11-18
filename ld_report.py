"""Late-dump tables. Usage: python ld_report.py SPLIT(dev|holdout) [cfg-regex] -> markdown on stdout, CSV in out/ld/"""
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat

OUT = Path(__file__).resolve().parent / "out" / "ld"
DAYS = {"dev": ["2026-11-14", "2026-11-15", "2026-11-16"], "holdout": ["2026-11-17"]}


def load(scn, days):
    z = [pd.read_csv(OUT / f"ld-{scn}-{d}-strict.csv.gz").assign(day=d) for d in days if (OUT / f"ld-{scn}-{d}-strict.csv.gz").exists()]
    return pd.concat(z) if z else pd.DataFrame()


def cell(z, nd):
    f = z[z.filled > 0]
    s = f[f.settle_c.notna()]
    r = dict(orders_day=len(z) / nd, fill_rate=len(f) / max(len(z), 1), shares_day=f.filled.sum() / nd)
    if len(s):
        r["settle"] = float((s.settle_c * s.filled).sum() / s.filled.sum())
        r["lo"], r["hi"] = pmlat.boot_ci(s.settle_c * s.filled, s.day + s.e.astype(str), weights=s.filled)
        r["dlo"], r["dhi"] = pmlat.boot_ci(s.settle_c * s.filled, s.day, weights=s.filled)
        lost = s[s.won == 0]
        r["lost_windows"] = len(lost)
        r["lost_usd"] = float((lost.settle_c * lost.filled).sum() / 100)
        r["usd_day"] = float((s.settle_c * s.filled).sum() / 100 / nd)
        r["price"] = float((s.price * s.filled).sum() / s.filled.sum() / 1e4)
    r["ahead_med"] = float(np.nanmedian(z.ahead)) if z.ahead.notna().any() else np.nan
    r["traded_med"] = float(z.traded.median())
    ok = z[z.ahead.notna()]
    r["queue_cleared"] = float((ok.traded >= ok.ahead).mean()) if len(ok) else np.nan
    return r


def fmt(v, p=2):
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.{p}f}"


def main():
    split = sys.argv[1]
    rx = re.compile(sys.argv[2]) if len(sys.argv) > 2 else None
    days = DAYS[split]
    data = {s: load(s, days) for s in ("live", "dublin")}
    rows = []
    for cfg in sorted(data["live"].cfg.unique()):
        if rx and not rx.search(cfg):
            continue
        for scn in ("live", "dublin"):
            z = data[scn][data[scn].cfg == cfg]
            if len(z):
                rows.append(dict(cfg=cfg, scn=scn, **cell(z, len(days))))
    T = pd.DataFrame(rows)
    T.to_csv(OUT / f"ld-table-{split}.csv", index=False)
    print(f"| cell side\\|T s\\|X\\|pull | scen | orders/day | fill rate | shares/day | avg price | settle ¢/sh | CI market | CI day | $/day @5 | favourite lost (windows / $) | queue ahead med | traded at our price med | queue cleared |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in T.itertuples():
        print(f"| {r.cfg} | {r.scn} | {r.orders_day:.0f} | {r.fill_rate:.2f} | {r.shares_day:.0f} | {getattr(r, 'price', np.nan):.3f} | {fmt(getattr(r, 'settle', np.nan))} | "
              f"[{fmt(getattr(r, 'lo', np.nan))}, {fmt(getattr(r, 'hi', np.nan))}] | [{fmt(getattr(r, 'dlo', np.nan))}, {fmt(getattr(r, 'dhi', np.nan))}] | "
              f"{fmt(getattr(r, 'usd_day', np.nan))} | {getattr(r, 'lost_windows', 0):.0f} / {fmt(getattr(r, 'lost_usd', np.nan))} | {r.ahead_med:,.0f} | {r.traded_med:,.0f} | {r.queue_cleared:.2f} |")


if __name__ == "__main__":
    main()
