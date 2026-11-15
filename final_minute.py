"""Final-minute calibration (descriptive): BTC/ETH 5m and 15m, last 60 s. At every whole second left (1..60), a token
whose ask is 0.90-0.99 with >= 5 shown is a hypothetical FAK buy at that ask. Bucket by ask x seconds left, and by a
TWAP-aware fair value: close = 60 s average of the oracle price at window end, open = 60 s average at window start.
FV(Up) = Phi((E[close] - open) / sd), E[close] = ((60 - tau) * A + tau * S) / 60, sd^2 = sigma^2 tau^3 / (3 * 60^2),
A = average so far over the last-60 s span, S = price now, sigma = $/sqrt(s) from 1 s changes over the prior 30 min.
Oracle proxy on 2026-11-14: Binance spot (no Chainlink recording that day). CI: market bootstrap 90%.
Usage: python final_minute.py --days 2026-11-14
"""
import argparse
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d

ASK_B = ((9000, 9500, "0.90-0.95"), (9500, 9700, "0.95-0.97"), (9700, 9900, "0.97-0.99"))
S_B = ((1, 5, "1-5s"), (6, 15, "6-15s"), (16, 30, "16-30s"), (31, 60, "31-60s"))
FV_B = ((0.0, 0.90, "<0.90"), (0.90, 0.95, "0.90-0.95"), (0.95, 0.97, "0.95-0.97"), (0.97, 0.99, "0.97-0.99"), (0.99, 1.01, "0.99+"))


def bucket(v, B):
    for lo, hi, n in B:
        if lo <= v < hi or (B is S_B and lo <= v <= hi):
            return n
    return None


def per_second(T, P, t0_ms, n):
    """Last Binance price at each whole second t0, t0+1s, ... (n values); NaN before the first trade."""
    q = t0_ms + 1000 * np.arange(n)
    i = np.searchsorted(T, q, "right") - 1
    return np.where(i >= 0, P[np.maximum(i, 0)], np.nan)


def fair_up(T, P, e, W, s_left):
    end = (e + W) * 1000
    t = end - 1000 * s_left
    op = np.nanmean(per_second(T, P, e * 1000 - 60_000 + 1000, 60))
    part = per_second(T, P, end - 60_000 + 1000, 60 - s_left)
    S = per_second(T, P, t, 1)[0]
    hist = per_second(T, P, t - 1_800_000, 1801)
    sig = np.nanstd(np.diff(hist))
    tau = s_left
    A = np.nanmean(part) if len(part) else S
    mu = ((60 - tau) * A + tau * S) / 60
    sd = sig * sqrt(tau ** 3 / (3 * 3600))
    if not np.isfinite(mu - op) or not sd > 0:
        return np.nan
    return 0.5 * (1 + erf((mu - op) / (sd * sqrt(2))))


def run(days):
    W_ = d.winners()
    rows = []
    for day in days:
        for asset in ("btc", "eth"):
            mk, _, bn = d.load_day(day, asset=asset)
            b = bn[bn["src"] == 0]
            o = np.argsort(b["T"], kind="stable")
            T, P = b["T"][o].astype(np.int64), b["p"][o]
            for (a, tf, e), m in mk.items():
                w = W_.get(d.slug((a, tf, e)))
                if w is None:
                    continue
                B = m["book"]
                for s in range(1, 61):
                    t = (e + tf * 60 - s) * 1000
                    i = d.state(m, t)
                    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):
                        continue
                    for tok, ask, sz in ((0, int(B["k"][i]), float(B["ks"][i])), (1, 10000 - int(B["b"][i]) if B["b"][i] else 0, float(B["bs"][i]))):
                        if not (9000 <= ask < 9900 and sz >= 5):
                            continue
                        fu = fair_up(T, P, e, tf * 60, s)
                        fv = fu if tok == 0 else 1 - fu
                        win = float((w == "UP") == (tok == 0))
                        fee_c = 100 * d.fee(ask, 1)
                        rows.append(dict(day=day, asset=a, tf=tf, e=e, s_left=s, tok=tok, ask=ask, fv=fv, win=win,
                                         cost_c=ask / 100 + fee_c, net_c=100 * win - ask / 100 - fee_c))
    return pd.DataFrame(rows)


def boot(v, cl, B=2000, seed=5):
    u, inv = np.unique(cl, return_inverse=True)
    S, N = np.bincount(inv, v, len(u)), np.bincount(inv, None, len(u))
    idx = np.random.default_rng(seed).integers(0, len(u), (B, len(u)))
    b = S[idx].sum(1) / np.maximum(N[idx].sum(1), 1)
    return np.percentile(b, [5, 95])


def table(R, by):
    out = []
    R = R.assign(mkt=R.asset + R.tf.astype(str) + R.e.astype(str))
    for g, G in R.groupby(["asset", "tf", by, "sb"]):
        lo, hi = boot(G.net_c.values, G.mkt.values)
        out.append(dict(asset=g[0], tf=g[1], bucket=g[2], s_left=g[3], n=len(G), markets=G.mkt.nunique(),
                        win=round(G.win.mean(), 4), ask_plus_fee_c=round(G.cost_c.mean(), 2), fv=round(G.fv.mean(), 4),
                        net_c=round(G.net_c.mean(), 2), lo90=round(lo, 2), hi90=round(hi, 2)))
    return pd.DataFrame(out)


def fmt(T, title):
    lines = [f"== {title}", "  asset tf  bucket      s_left   n(snap) mkts  win%    ask+fee  FV     net c/share [90% CI]"]
    for r in T.itertuples():
        lines.append(f"  {r.asset:4s} {r.tf:>2}m {r.bucket:10s} {r.s_left:7s} {r.n:7d} {r.markets:4d} {100 * r.win:6.2f}  {r.ask_plus_fee_c:6.2f}"
                     f"  {r.fv:.3f}  {r.net_c:+6.2f} [{r.lo90:+.2f}, {r.hi90:+.2f}]")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", default=["2026-11-14"])
    ap.add_argument("--out", default=str(Path.home() / "dublin-retest" / "out" / "final-minute"))
    a = ap.parse_args()
    if any(x != "2026-11-14" for x in a.days):
        raise SystemExit("2026-11-14 only until the operator confirms (overlap with the registered late-breakout test)")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    R = run(a.days)
    R["ab"] = [bucket(x, ASK_B) for x in R.ask]
    R["sb"] = [bucket(x, S_B) for x in R.s_left]
    R["fb"] = [bucket(x, FV_B) if np.isfinite(x) else "nan" for x in R.fv]
    R.to_csv(Path(a.out) / "snapshots.csv.gz", index=False)
    TA, TF = table(R, "ab"), table(R, "fb")
    TA.to_csv(Path(a.out) / "by-ask.csv", index=False)
    TF.to_csv(Path(a.out) / "by-fv.csv", index=False)
    print(fmt(TA, f"final minute by ask, {a.days} (snapshots every 1 s; CI by market)"))
    print(fmt(TF, f"final minute by TWAP-aware fair value (Binance proxy), {a.days}"))
