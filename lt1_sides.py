"""LT1 side models per window (frozen/LT1_plan.json "side_models"). Inputs strictly before the window start:
Gamma winners of earlier windows, Binance spot BTC trades with box receive time < window start.
Side: 0 UP, 1 DOWN, -1 skip. Usage: python lt1_sides.py DAY [DAY ...] -> out/lt1/sides-DAY.csv
"""
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

import lt1_sim as L
import pmlat
import dsim


def spot(days):
    parts = []
    for d in days:
        for f in sorted((pmlat.REC / d).glob("bn-*.bin")):
            a = np.fromfile(f, dtype=dsim.BN_DT)
            a = a[(a["a"] == 0) & (a["src"] == 0)]
            parts.append(np.c_[(a["l"] // 1000).astype(np.float64), a["p"]])
    z = np.concatenate(parts) if parts else np.zeros((0, 2))
    return z[np.argsort(z[:, 0], kind="stable")]


def last_before(S, t):
    i = np.searchsorted(S[:, 0], t, "left") - 1
    return np.where(i >= 0, S[np.clip(i, 0, None), 1], np.nan), np.where(i >= 0, S[np.clip(i, 0, None), 0], np.nan)


def sides_day(day, W):
    d0 = date.fromisoformat(day)
    S = spot([(d0 - timedelta(days=1)).isoformat(), day])
    t_day = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
    out = []
    for tf in (5, 15):
        for e in range(t_day, t_day + 86400, tf * 60):
            t0 = e * 1000
            rng = np.random.default_rng([20261126, tf, e])
            R = int(rng.integers(0, 2))
            w1, w2 = W.get(L.slug(tf, e - tf * 60)), W.get(L.slug(tf, e - 2 * tf * 60))
            S1 = -1 if w1 is None else int(w1 != "UP")
            S2 = -1 if (w1 is None or w2 is None or w1 != w2) else int(w1 != "UP")
            p, tt = last_before(S, np.array([t0, t0 - 15 * 60000, t0 - 60 * 60000], float))
            fresh = lambda k, ref: np.isfinite(p[k]) and ref - tt[k] < 60000  # noqa: E731  (a trade within the minute before)
            M15 = -1 if not (fresh(0, t0) and fresh(1, t0 - 900000)) or p[0] == p[1] else int(p[0] < p[1])
            M60 = -1 if not (fresh(0, t0) and fresh(2, t0 - 3600000)) or p[0] == p[2] else int(p[0] < p[2])
            b = t0 - 60000 * np.arange(59, -1, -1)  # 60 minute boundaries, oldest first; close = last trade before boundary
            c, ct = last_before(S, b.astype(float))
            ok = np.isfinite(c) & (b - ct < 60000)
            if ok.sum() >= 40 and ok[-1]:
                cc = c[ok]
                ema = cc[0]
                for v in cc[1:]:
                    ema += (2 / 21) * (v - ema)
                EMA = -1 if cc[-1] == ema else int(cc[-1] < ema)
            else:
                EMA = -1
            out.append((day, tf, e, R, S1, S2, M15, M60, EMA))
    return out


if __name__ == "__main__":
    W = pmlat.winners([])
    L.OUT.mkdir(parents=True, exist_ok=True)
    for day in sys.argv[1:]:
        d0 = date.fromisoformat(day)
        t_day = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
        miss = [L.slug(tf, e) for tf in (5, 15) for e in range(t_day - 2 * tf * 60, t_day + 86400, tf * 60) if L.slug(tf, e) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        rows = sides_day(day, W)
        with open(L.OUT / f"sides-{day}.csv", "w") as f:
            f.write("day,tf,e,R,S1,S2,M15,M60,EMA\n")
            for r in rows:
                f.write(",".join(map(str, r)) + "\n")
        a = np.array([r[3:] for r in rows])
        print(day, len(rows), "skip share R/S1/S2/M15/M60/EMA", np.round((a < 0).mean(0), 3), flush=True)
