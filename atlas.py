"""Minimal speed atlas (stand-in for the missing speed_atlas.py): Binance >= M bp moves vs Polymarket book reaction.
Event: Binance trade at T where |p / p(T - 500 ms) - 1| >= M bp, 2 s debounce per asset. For every live window of that
asset (mid 0.05..0.95, >= 10 s left): reprice delay = first book row after T whose mid moved >= half a tick in the move
direction (exchange ms - Binance ms, both venue clocks); stale = the ask toward the move at T, hittable at T + d if the
book (strictly before T + d) still offers <= that price with >= 5 shown.
Usage: python atlas.py --day 2026-11-14 --src spot|fut
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d

DELAYS = (130, 160, 200)


def events(bn, src, ai, m_bp):
    b = bn[(bn["src"] == src) & (bn["a"] == ai)]
    b = b[np.argsort(b["T"], kind="stable")]
    T, p = b["T"], b["p"]
    j = np.searchsorted(T, T - 500, "left")
    r = (p / p[j] - 1) * 1e4
    out, last = [], -10**15
    for i in np.flatnonzero(np.abs(r) >= m_bp):
        if T[i] - last >= 2000:
            out.append((int(T[i]), int(np.sign(r[i])), float(r[i])))
            last = T[i]
    return out


def react(m, t0, sgn):
    B = m["book"]
    i = d.state(m, t0)
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return None
    mid0 = (int(B["b"][i]) + int(B["k"][i])) / 2
    if not 500 <= mid0 <= 9500:
        return None
    # stale ask toward the move: up -> UP ask; down -> DOWN ask = 1 - UP bid (kept in UP terms: the UP bid)
    lvl = int(B["k"][i]) if sgn > 0 else int(B["b"][i])
    j1 = np.searchsorted(B["x"], t0 + 10_000, "left")
    s = slice(i + 1, j1)
    bb, kk, xx = B["b"][s].astype(int), B["k"][s].astype(int), B["x"][s]
    mids = (bb + kk) / 2
    ok = (bb > 0) & (kk > 0) & ((mids - mid0) * sgn >= 50)
    delay = float(xx[np.argmax(ok)] - t0) if ok.any() else np.nan
    row = dict(delay=delay)
    for dt in DELAYS:
        k = d.state(m, t0 + dt)
        if B["b"][k] == 0 and B["k"][k] == 0:
            row[f"hit{dt}"] = np.nan
            continue
        if sgn > 0:
            row[f"hit{dt}"] = float(B["k"][k] != 0 and B["k"][k] <= lvl and B["ks"][k] >= 5)
        else:
            row[f"hit{dt}"] = float(B["b"][k] != 0 and B["b"][k] >= lvl and B["bs"][k] >= 5)
    return row


def run(day, src, m_bp=2.0):
    mk, _, bn = d.load_day(day, shift=0)
    rows = []
    for ai, a in enumerate(d.ASSETS):
        for t0, sgn, r in events(bn, 0 if src == "spot" else 1, ai, m_bp):
            for tf in (5, 15):
                e = (t0 // 1000) // (tf * 60) * (tf * 60)
                m = mk.get((a, tf, e))
                if m is None or e + tf * 60 - t0 / 1000 < 10:
                    continue
                x = react(m, t0, sgn)
                if x:
                    rows.append(dict(asset=a, tf=tf, e=e, t0=t0, sgn=sgn, move_bp=r, **x))
    return pd.DataFrame(rows)


def summary(R, src):
    out = [f"== src={src}: events x live windows n={len(R)} (>= 2 bp in 500 ms, 2 s debounce)"]
    g = R.groupby(["asset", "tf"])
    t = pd.DataFrame({"n": g.size(), "repriced_10s": g.delay.apply(lambda s: s.notna().mean()).round(2),
                      "delay_p25": g.delay.quantile(0.25), "delay_p50": g.delay.median(), "delay_p75": g.delay.quantile(0.75)})
    for dt in DELAYS:
        t[f"stale@{dt}"] = g[f"hit{dt}"].mean().round(3)
    out.append("(a)+(b) per asset x tf: reprice delay ms (Binance trade time -> PM book exchange time) and share of events"
               " where the stale ask toward the move is still hittable (>= 5 shown) at t0 + d")
    out.append(t.round(1).to_string())
    tot = {f"stale@{dt}": round(float(R[f'hit{dt}'].mean()), 3) for dt in DELAYS}
    out.append(f"all: delay p50 {R.delay.median():.0f} ms, {tot}")
    # (c) leadership
    P = R.pivot_table(index=["asset", "t0"], columns="tf", values="delay")
    if 5 in P and 15 in P:
        both = P.dropna()
        out.append(f"(c1) 5m vs 15m same asset+event: n={len(both)}, 5m first {np.mean(both[5] < both[15]):.2f}, "
                   f"tie {np.mean(both[5] == both[15]):.2f}, median(15m - 5m) {np.median(both[15] - both[5]):.0f} ms")
    R5 = R[R.tf == 5]
    btc = R5[R5.asset == "btc"].set_index("t0").delay
    for a in ("eth", "sol", "xrp", "doge"):
        alt = R5[R5.asset == a].set_index("t0").delay
        # pair each BTC event with the alt event within 300 ms (same market move)
        bt, at = btc.dropna(), alt.dropna()
        if not len(bt) or not len(at):
            continue
        ai = np.searchsorted(at.index.values, bt.index.values)
        pairs = []
        for k, t in enumerate(bt.index.values):
            for c in (ai[k] - 1, ai[k]):
                if 0 <= c < len(at) and abs(at.index.values[c] - t) <= 300:
                    pairs.append((t + bt.values[k], at.index.values[c] + at.values[c]))
                    break
        if pairs:
            pr = np.array(pairs)
            out.append(f"(c2) BTC vs {a.upper()} 5m books, same move (Binance events within 300 ms): n={len(pr)}, BTC book "
                       f"reprices first {np.mean(pr[:, 0] < pr[:, 1]):.2f}, median(alt - BTC) reprice time "
                       f"{np.median(pr[:, 1] - pr[:, 0]):.0f} ms")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-14")
    ap.add_argument("--src", default="spot", choices=("spot", "fut"))
    ap.add_argument("--out", default=str(Path.home() / "dublin-retest" / "out" / "atlas"))
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    R = run(a.day, a.src)
    R.to_csv(Path(a.out) / f"atlas-{a.day}-{a.src}.csv", index=False)
    print(summary(R, a.src))
