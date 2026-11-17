"""Tables for p2-style outputs (one row per filled order with mo*, settle_c, rebate_c, imb_entry, imb_fill).
Usage: python p2_report.py PREFIX [days...]   e.g. PREFIX = p2-btc15 (reads out/p2/{PREFIX}-{scn}-{day}-strict{TAG}.csv.gz)
env P2_TAG selects a tag. Prints: main table (live and dublin side by side), imbalance-at-entry vs at-fill table."""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat

OUT = Path(__file__).resolve().parent / "out" / "p2"
DEV = ["2026-11-14", "2026-11-15", "2026-11-16"]


def load(prefix, scn, days, tag):
    F, O = [], []
    for d in days:
        b = OUT / f"{prefix}-{scn}-{d}-strict{tag}"
        if not Path(f"{b}-orders.csv").exists():  # run not finished yet
            continue
        f, o = pd.read_csv(f"{b}.csv.gz"), pd.read_csv(f"{b}-orders.csv")
        f["day"], o["day"] = d, d
        F.append(f), O.append(o)
    return (pd.concat(F) if F else pd.DataFrame()), (pd.concat(O) if O else pd.DataFrame())


def wmean(z, c):
    s = z[z[c].notna()]
    return float((s[c] * s.filled).sum() / s.filled.sum()) if len(s) else np.nan


def summ(f, ndays):
    s = f[f.settle_c.notna()]
    lo, hi = pmlat.boot_ci(s.settle_c * s.filled, s.day + s.e.astype(str), weights=s.filled)
    dlo, dhi = pmlat.boot_ci(s.settle_c * s.filled, s.day, weights=s.filled)
    return dict(fills_day=len(f) / ndays, mo1=wmean(f, "mo1000"), mo5=wmean(f, "mo5000"), mo30=wmean(f, "mo30000"),
                mo60=wmean(f, "mo60000"), settle=wmean(f, "settle_c"), lo=lo, hi=hi, dlo=dlo, dhi=dhi, rebate=wmean(f, "rebate_c"))


def fmt(v, p=2):
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.{p}f}"


def main():
    prefix = sys.argv[1]
    days = sys.argv[2:] or DEV
    tag = os.environ.get("P2_TAG", "")
    data = {scn: load(prefix, scn, days, tag) for scn in ("live", "dublin")}
    cfgs = sorted(set(data["live"][1].cfg) if len(data["live"][1]) else set())
    print(f"| config | scenario | orders/day | fill rate | fills/day | mo 1 s | 5 s | 30 s | 60 s | settle ¢/sh | CI market | CI day | rebate ¢ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    rows = []
    for cfg in cfgs:
        for scn in ("live", "dublin"):
            F, O = data[scn]
            if not len(O):
                continue
            o = O[O.cfg == cfg]
            f = F[F.cfg == cfg]
            if not len(f):
                continue
            S = summ(f, len(days))
            rows.append(dict(cfg=cfg, scn=scn, **S))
            print(f"| {cfg} | {scn} | {o.orders.sum() / len(days):,.0f} | {o.filled_orders.sum() / max(o.orders.sum(), 1):.2f} | {S['fills_day']:,.0f} | "
                  f"{fmt(S['mo1'])} | {fmt(S['mo5'])} | {fmt(S['mo30'])} | {fmt(S['mo60'])} | {fmt(S['settle'])} | "
                  f"[{fmt(S['lo'])}, {fmt(S['hi'])}] | [{fmt(S['dlo'])}, {fmt(S['dhi'])}] | {S['rebate']:.2f} |")
    pd.DataFrame(rows).to_csv(OUT / f"{prefix}-table{tag}.csv", index=False)
    # imbalance at entry vs at fill (live)
    F = data["live"][0]
    if len(F) and "imb_entry" in F:
        print("\nOwn imbalance at entry vs at fill (live, all configs pooled; settle ¢/share, 1 s / 30 s markout)\n")
        print("| imbalance bucket | at entry: fills | settle | mo1 | mo30 | at fill: fills | settle | mo1 | mo30 |")
        print("|---|---|---|---|---|---|---|---|---|")
        bins = [-1.01, -0.6, -0.3, 0, 0.3, 0.6, 1.01]
        lab = ["< -0.6", "-0.6..-0.3", "-0.3..0", "0..0.3", "0.3..0.6", ">= 0.6"]
        F = F.assign(be=pd.cut(F.imb_entry, bins, labels=lab, right=False), bf=pd.cut(F.imb_fill, bins, labels=lab, right=False))
        for lb in lab:
            a, b = F[F.be == lb], F[F.bf == lb]
            print(f"| {lb} | {len(a):,} | {fmt(wmean(a, 'settle_c'))} | {fmt(wmean(a, 'mo1000'))} | {fmt(wmean(a, 'mo30000'))} | "
                  f"{len(b):,} | {fmt(wmean(b, 'settle_c'))} | {fmt(wmean(b, 'mo1000'))} | {fmt(wmean(b, 'mo30000'))} |")


if __name__ == "__main__":
    main()
