"""Shared row stats for the F7i latency / F8 tables: fills/day, fill rate, settle c/share (share-weighted) with a 90%
market-bootstrap CI, share-weighted 1 s markout, and the average pair cost per window (avg UP price + avg DOWN price,
share-weighted within the window, over windows where both tokens filled)."""
import numpy as np

import pmlat

HDR = ("| orders/day | fill rate | fills/day | settle c/share | 90% CI (market) | mo1s c/share | $/day | "
       "pair cost/window (n windows) |")
SEP = "|---:|---:|---:|---:|---|---:|---:|---|"


def stats(F, orders, ndays):
    """F: filled rows (cols day, e, tok, px [1e-4], filled, settle_c, mo1000); orders: number of orders posted."""
    if len(F) < 2:
        return dict(n=len(F), cells=f"| {orders / ndays:,.0f} | | {len(F) / ndays:,.0f} | n<2 | | | | |", lo=np.nan, net=np.nan, mo=np.nan, fpd=len(F) / ndays)
    ok = F.settle_c.notna().to_numpy()
    w, v = F.filled.to_numpy(float)[ok], F.settle_c.to_numpy(float)[ok]
    key = (F.day.astype(str) + ":" + F.e.astype(str)).to_numpy()
    lo, hi = pmlat.boot_ci(v * w, key[ok], weights=w)
    net = (v * w).sum() / w.sum()
    mm = F.mo1000.notna().to_numpy()
    mo = (F.mo1000.to_numpy(float)[mm] * F.filled.to_numpy(float)[mm]).sum() / F.filled.to_numpy(float)[mm].sum()
    G = F.assign(k=key, pw=F.px * F.filled).groupby(["k", "tok"])[["pw", "filled"]].sum()
    avg = (G.pw / G.filled).unstack()
    both = avg.dropna() if avg.shape[1] == 2 else avg.iloc[0:0]
    pair = (both[0] + both[1]).mean() / 1e4 if len(both) else np.nan
    nwin = len(set(key))
    cells = (f"| {orders / ndays:,.0f} | {len(F) / max(orders, 1):.2f} | {len(F) / ndays:,.0f} | {net:+.2f} | [{lo:+.2f}, {hi:+.2f}] | "
             f"{mo:+.2f} | {(v * w).sum() / 100 / ndays:+,.0f} | {pair:.3f} ({len(both)}/{nwin}) |")
    return dict(n=len(F), cells=cells, lo=lo, hi=hi, net=net, mo=mo, fpd=len(F) / ndays, pair=pair)
