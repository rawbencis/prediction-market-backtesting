"""Lead-lag of Polymarket BTC 5m up/down books against Binance BTCUSDT (uses the same_token_wf2 cache).

btc:     Binance daily archives (spot 1 s klines, or spot / USD-M aggTrades), sha256-checked ->
         per-day last-price grid at RES ms (bin i = last trade at or before the END of bin i, ffilled).
extract: per market: Binance open / close / TWAP vs Gamma winner (settlement-rule check); 1 s rows of
         fair p_up = N(ln(S_t/S_0) / (sig*sqrt(tau))) vs the books, future UP mids, asks after
         latency, exit bids; per-day cross-correlation of d(fair) vs d(UP mid) at lags -LAGS..LAGS.
stats:   rule check, calibration / Brier (fair vs mid), logit(win) ~ logit(mid) + logit(fair) at fixed t,
         lead-lag profile, gap closure, taker trade test: first signal per market, IOC at the signal
         ask arriving after latency, fee rate*p*(1-p) per share, held to settlement or sold at the bid
         after h s. Model scale and thresholds are chosen on pre-holdout days, reported on the holdout.

Book times are the recorder's receipt times; Binance times are exchange times. Measured lag therefore
includes the recorder's network delay to Polymarket (not to Binance).
"""

import argparse
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import same_token_wf2 as wf

try:
    from scipy.special import ndtr
except ImportError:  # pragma: no cover
    _erf = np.vectorize(math.erf, otypes=[float])

    def ndtr(x):
        return 0.5 * (1 + _erf(np.asarray(x, float) / math.sqrt(2)))


BASE = "https://data.binance.vision/data/"
SRC = {  # path, time col, price col
    "spot1s": ("spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{d}.zip", 6, 4),
    "spotagg": ("spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip", 5, 1),
    "umagg": ("futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip", 5, 1),
}
DAY_MS = 86_400_000
T_ROWS = (5, 296)  # row times, s from market open
HS = (1, 2, 5, 10, 30)  # future-mid horizons, s
LATS = (100, 250, 1000)  # order arrival latency, ms
EXITS = (5, 30)  # taker exit after h s
XC_WIN = (10, 290)  # cross-correlation window, s
XC_MAX_S = 10  # max lag, s
SIZE = wf.SIZE


def cols():
    c = ["t", "sig", "z", "ub", "ua", "db", "da", "uasz", "dasz", "w"]
    c += [f"m{h}" for h in HS]
    c += [f"{s}{L}" for L in LATS for s in ("ua", "da", "uasz", "dasz")]
    c += [f"{s}{L}_{h}" for L in LATS for h in EXITS for s in ("ub", "db")]
    return c


COLS = cols()


def day_ms(d):
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- btc


def build_grid(t_ms, px, d0, res):
    """Trades (time ms, price) -> float64[DAY_MS/res], last price at or before each bin end, ffilled."""
    n = DAY_MS // res
    g = np.full(n, np.nan)
    o = np.argsort(t_ms, kind="stable")
    t_ms, px = t_ms[o], px[o]
    b = (t_ms - d0) // res
    k = (b >= 0) & (b < n)
    b, px = b[k], px[k]
    if len(b):
        last = np.r_[b[1:] != b[:-1], True]
        g[b[last]] = px[last]
    return ffill(g)


def ffill(g):
    f = np.isfinite(g)
    if not f.any():
        return g.copy()
    idx = np.where(f, np.arange(len(g)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = g[idx]
    out[: np.argmax(f)] = np.nan
    return out


def read_trades(path, ti, pi):
    df = pd.read_csv(path, header=None, usecols=[ti, pi], dtype=str)
    t = pd.to_numeric(df[ti], errors="coerce")
    p = pd.to_numeric(df[pi], errors="coerce")
    ok = t.notna() & p.notna()
    t = t[ok].to_numpy(np.int64)
    if len(t) and np.median(t) > 1e14:  # microsecond archives (spot from 2025)
        t = t // 1000
    return t, p[ok].to_numpy(float)


def fetch(url, dst):
    req = lambda u: urllib.request.urlopen(  # noqa: E731
        urllib.request.Request(u, headers={"User-Agent": "research-backtest"}), timeout=120
    )
    for k in range(5):
        try:
            with req(url + ".CHECKSUM") as r:
                want = r.read().decode().split()[0]
            h = hashlib.sha256()
            tmp = dst.with_suffix(".part")
            with req(url) as r, tmp.open("wb") as f:
                while chunk := r.read(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
            if h.hexdigest() != want:
                raise OSError(f"sha256 mismatch {url}")
            tmp.replace(dst)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            err = e
        except OSError as e:
            err = e
        time.sleep(2**k)
    raise err


def cmd_btc(a):
    days = a.days or [d.name for d in wf.done_days(a.cache)]
    if not days:
        sys.exit("no cache days")
    days = sorted(set(days) | {(date.fromisoformat(days[0]) - timedelta(1)).isoformat()})
    path, ti, pi = SRC[a.src]
    out = a.btc / f"{a.src}_{a.res}"
    raw = a.btc / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    for d in days:
        f = out / f"{d}.npy"
        if f.exists() and not a.force:
            continue
        z = raw / Path(path.format(d=d)).name
        if not z.exists() and not fetch(BASE + path.format(d=d), z):
            print(d, "missing on Binance", flush=True)
            continue
        t, p = read_trades(z, ti, pi)
        g = build_grid(t, p, day_ms(d), a.res)
        np.save(f, g)
        if not a.keep_zip:
            z.unlink()
        print(d, f"trades={len(t)} filled={np.isfinite(g).mean():.4f}", flush=True)


# ---------------------------------------------------------------- extract


def book(m, q, age=wf.AGE_US):
    """Book state at query times q (us from open) -> bid, ask, bsz, asz (n,2) in $, NaN if not fresh."""
    q = np.asarray(q, np.int64)
    i = np.searchsorted(m["t"], q, "right") - 1
    ii = np.maximum(i, 0)
    ok = (
        (i >= 0)[:, None]
        & (m["bid"][ii] >= 0)
        & ((q[:, None] - m["seen"][ii]) <= age)
        & (q < wf.MKT_US)[:, None]
    )
    f = lambda x, s=1.0: np.where(ok, x[ii] * s, np.nan)  # noqa: E731
    return f(m["bid"], 1e-3), f(m["ask"], 1e-3), f(m["bsz"]), f(m["asz"])


class Btc:
    def __init__(self, gdir, day, res):
        prev = (date.fromisoformat(day) - timedelta(1)).isoformat()
        n = DAY_MS // res
        parts = [gdir / f"{d}.npy" for d in (prev, day)]
        self.g = ffill(np.concatenate([np.load(p) if p.exists() else np.full(n, np.nan) for p in parts]))
        self.t0 = day_ms(prev)
        self.res = res

    def at(self, t_ms):
        """Last price known at t_ms (bin ending at or before t_ms)."""
        i = (np.asarray(t_ms, np.int64) - self.t0) // self.res - 1
        ok = (i >= 0) & (i < len(self.g))
        return np.where(ok, self.g[np.clip(i, 0, len(self.g) - 1)], np.nan)

    def sigma(self, e_ms, win_s):
        """Per-sqrt-second vol from bin log returns over [e - win, e)."""
        a = (e_ms - win_s * 1000 - self.t0) // self.res
        b = (e_ms - self.t0) // self.res
        if a < 0:
            return np.nan
        r = np.diff(np.log(self.g[a:b]))
        f = np.isfinite(r)
        if f.mean() < 0.5:
            return np.nan
        return math.sqrt(np.sum(r[f] ** 2) / (f.sum() * self.res / 1000))

    def twap(self, e_ms):
        a = (e_ms - self.t0) // self.res
        return np.nanmean(self.g[a : a + wf.MKT_US // 1000 // self.res])


def fair_z(btc, e_ms, ts, s0, sig):
    s = btc.at(e_ms + np.rint(np.asarray(ts) * 1000).astype(np.int64))
    tau = 300.0 - np.asarray(ts, float)
    return np.log(s / s0) / (sig * np.sqrt(tau))


def market_rows(m, epoch, w, btc, vol_win, res):
    e = epoch * 1000
    s0, se = btc.at(e), btc.at(e + 300_000)
    sig = btc.sigma(e, vol_win)
    mk = (epoch, -1 if w is None else w, s0, se, btc.twap(e), sig)
    if not (np.isfinite(s0) and np.isfinite(sig) and sig > 0):
        return mk, None, None
    ts = np.arange(*T_ROWS, 1.0)
    q = np.rint(ts * 1e6).astype(np.int64)
    z = fair_z(btc, e, ts, s0, sig)
    bid, ask, _, asz = book(m, q)
    R = {"t": ts, "sig": np.full(len(ts), sig), "z": z, "w": np.full(len(ts), -1 if w is None else w)}
    R.update(ub=bid[:, 0], ua=ask[:, 0], db=bid[:, 1], da=ask[:, 1], uasz=asz[:, 0], dasz=asz[:, 1])
    for h in HS:
        b, a_, _, _ = book(m, q + h * 1_000_000)
        R[f"m{h}"] = (b[:, 0] + a_[:, 0]) / 2
    for L in LATS:
        _, a_, _, s_ = book(m, q + L * 1000)
        R.update({f"ua{L}": a_[:, 0], f"da{L}": a_[:, 1], f"uasz{L}": s_[:, 0], f"dasz{L}": s_[:, 1]})
        for h in EXITS:
            b, _, _, _ = book(m, q + L * 1000 + h * 1_000_000)
            R[f"ub{L}_{h}"], R[f"db{L}_{h}"] = b[:, 0], b[:, 1]
    # cross-correlation on the BTC grid
    tb = np.arange(XC_WIN[0], XC_WIN[1], res / 1000)
    f = ndtr(fair_z(btc, e, tb, s0, sig))
    b, a_, _, _ = book(m, np.rint(tb * 1e6).astype(np.int64))
    df, dm = np.diff(f), np.diff((b[:, 0] + a_[:, 0]) / 2)
    nl = XC_MAX_S * 1000 // res
    xc = np.zeros((2 * nl + 1, 4))
    for j, lag in enumerate(range(-nl, nl + 1)):
        x = df[max(0, -lag) : len(df) - max(0, lag)]
        y = dm[max(0, lag) : len(dm) - max(0, -lag)]
        k = np.isfinite(x) & np.isfinite(y)
        x, y = x[k], y[k]
        xc[j] = (x @ y, x @ x, y @ y, len(x))
    return mk, R, xc


def _extract_day(args):
    ddir, out, gdir, res, sett, vol_win = args
    btc = Btc(gdir, ddir.name, res)
    mks, rows, xc = [], {c: [] for c in COLS}, 0
    ep = []
    for epoch, m in wf.markets(ddir):
        mk, R, x = market_rows(m, epoch, sett.get(epoch), btc, vol_win, res)
        mks.append(mk)
        if R is None:
            continue
        ep.append(np.full(len(R["t"]), epoch, np.int64))
        for c in COLS:
            rows[c].append(np.asarray(R[c], np.float32))
        xc = xc + x
    if not ep:
        return ddir.name, 0
    tmp = out / f".{ddir.name}.tmp"
    with tmp.open("wb") as fh:
        np.savez(
            fh,
            epoch=np.concatenate(ep),
            mk=np.array(mks, float),
            xc=np.asarray(xc, float),
            **{c: np.concatenate(v) for c, v in rows.items()},
        )
    tmp.replace(out / f"{ddir.name}.npz")
    return ddir.name, len(mks)


def cmd_extract(a):
    gdir = a.btc / f"{a.src}_{a.res}"
    sett = wf.settlements(a.cache)
    a.out.mkdir(parents=True, exist_ok=True)
    jobs = [
        (d, a.out, gdir, a.res, sett, a.vol_win)
        for d in wf.done_days(a.cache)
        if (gdir / f"{d.name}.npy").exists() and (a.force or not (a.out / f"{d.name}.npz").exists())
    ]
    print(f"{len(jobs)} days to extract; winners known for {len(sett)} markets", flush=True)
    (a.out / "meta.json").write_text(json.dumps({"src": a.src, "res": a.res, "vol_win": a.vol_win}))
    with Pool(a.workers, maxtasksperchild=4) as pool:
        for d, n in pool.imap_unordered(_extract_day, jobs):
            print(d, n, flush=True)


# ---------------------------------------------------------------- stats


def fee(p, rate):
    return rate * p * (1 - p)


def boot(x, n=2000, seed=0):
    x = np.asarray(x, float)
    if len(x) == 0:
        return [np.nan] * 3
    r = np.random.default_rng(seed)
    bs = x[r.integers(0, len(x), (n, len(x)))].mean(1)
    return [float(x.mean()), float(np.percentile(bs, 5)), float(np.percentile(bs, 95))]


def logit(p):
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1 - p))


def logreg(X, y, it=25):
    X = np.column_stack([np.ones(len(y)), X])
    b = np.zeros(X.shape[1])
    for _ in range(it):
        p = 1 / (1 + np.exp(-X @ b))
        W = p * (1 - p) + 1e-9
        H = X.T @ (X * W[:, None])
        b = b + np.linalg.solve(H, X.T @ (y - p))
    return b, np.sqrt(np.diag(np.linalg.inv(H)))


def loglik(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def load(out, keys):
    fs = sorted(out.glob("????-??-??.npz"))
    D = {}
    for f in fs:
        with np.load(f) as z:
            D[f.stem] = {k: z[k] for k in keys}
    return D


def entries(d, k, theta, win, L, rate):
    """First filled taker entry per market. -> list of (row, side, price)."""
    fair = ndtr(k * d["z"])
    ua, da = d["ua"].astype(float), d["da"].astype(float)
    eu = np.nan_to_num(fair - ua - fee(ua, rate), nan=-9)
    ed = np.nan_to_num((1 - fair) - da - fee(da, rate), nan=-9)
    side = (ed > eu).astype(int)
    sig = (np.maximum(eu, ed) > theta) & (d["t"] >= win[0]) & (d["t"] < win[1])
    arr = np.where(side == 0, d[f"ua{L}"], d[f"da{L}"])
    asz = np.where(side == 0, d[f"uasz{L}"], d[f"dasz{L}"])
    lim = np.where(side == 0, ua, da)
    fill = sig & np.isfinite(arr) & (arr <= lim + 1e-9) & (asz >= SIZE)
    out = []
    ep, t = d["epoch"], d["t"]
    idx = np.flatnonzero(sig)
    if not len(idx):
        return out
    starts = np.r_[0, np.flatnonzero(ep[idx][1:] != ep[idx][:-1]) + 1, len(idx)]
    for a_, b_ in zip(starts[:-1], starts[1:]):
        nxt = -1.0
        for r in idx[a_:b_]:
            if t[r] < nxt:
                continue
            if fill[r]:
                out.append((r, side[r], float(arr[r])))
                break
            nxt = t[r] + L / 1000
    return out


def trade_pnl(d, ent, L, ex, rate):
    """Per-share pnl for entries; ex = 'settle' or exit horizon h (falls back to settlement)."""
    pn = []
    for r, s, p in ent:
        w = d["w"][r]
        if w < 0:
            continue
        settle = (1.0 if w == s else 0.0) - p - fee(p, rate)
        if ex == "settle":
            pn.append(settle)
            continue
        b = d[f"{'ub' if s == 0 else 'db'}{L}_{ex}"][r]
        pn.append(b - fee(b, rate) - p - fee(p, rate) if np.isfinite(b) else settle)
    return pn


def cmd_stats(a):
    meta = json.loads((a.out / "meta.json").read_text())
    keys = ["epoch", "mk", "xc"] + COLS
    D = load(a.out, keys)
    days = sorted(D)
    if len(days) <= a.holdout_days:
        sys.exit(f"{len(days)} days; need > {a.holdout_days}")
    dev, hold = days[: -a.holdout_days], days[-a.holdout_days :]
    S = {"meta": meta, "days": [days[0], days[-1]], "holdout_start": hold[0], "fee_rate": a.fee_rate}

    # 1. settlement-rule check
    mk = np.concatenate([D[x]["mk"] for x in days])
    mk = mk[(mk[:, 1] >= 0) & np.isfinite(mk[:, 2:5]).all(1)]
    up = mk[:, 1] == 0
    mv = np.abs(mk[:, 3] / mk[:, 2] - 1) * 1e4
    S["rule_check"] = {
        "markets": int(len(mk)),
        "close_vs_open_agree": float(((mk[:, 3] >= mk[:, 2]) == up).mean()),
        "twap_vs_open_agree": float(((mk[:, 4] >= mk[:, 2]) == up).mean()),
        "close_agree_by_abs_move_bp": {
            f"{lo}-{hi}": float(((mk[k, 3] >= mk[k, 2]) == up[k]).mean())
            for lo, hi in ((0, 1), (1, 2), (2, 5), (5, 10), (10, 1e9))
            if (k := (mv >= lo) & (mv < hi)).any()
        },
        "sigma_per_s_bp_median": float(np.nanmedian(mk[:, 5]) * 1e4),
    }

    # 2. calibration: model scale k (dev only), Brier / loglik by t, logistic at fixed t
    def rows_at(ds, t):
        z, m_, w = [], [], []
        for x in ds:
            d = D[x]
            k = (d["t"] == t) & (d["w"] >= 0) & np.isfinite(d["z"]) & np.isfinite(d["ub"] + d["ua"])
            z.append(d["z"][k])
            m_.append((d["ub"][k] + d["ua"][k]) / 2)
            w.append((d["w"][k] == 0).astype(float))
        return np.concatenate(z), np.concatenate(m_), np.concatenate(w)

    TS = (30, 60, 120, 180, 240, 270, 285, 295)
    zd, _, wd = (np.concatenate(v) for v in zip(*[rows_at(dev, t) for t in TS]))
    ks = np.round(np.arange(0.4, 1.81, 0.05), 2)
    kk = float(ks[np.argmax([loglik(ndtr(k * zd), wd) for k in ks])])
    S["model_scale_k_dev"] = kk
    cal = {}
    for t in TS:
        z, mid, w = rows_at(days, t)
        fr = ndtr(kk * z)
        b, se = logreg(np.column_stack([logit(mid), logit(fr)]), w)
        cal[t] = {
            "n": int(len(w)),
            "brier_mid": float(np.mean((mid - w) ** 2)),
            "brier_fair": float(np.mean((fr - w) ** 2)),
            "loglik_mid": loglik(mid, w),
            "loglik_fair": loglik(fr, w),
            "logit_coef_mid": [round(float(b[1]), 3), round(float(se[1]), 3)],
            "logit_coef_fair": [round(float(b[2]), 3), round(float(se[2]), 3)],
        }
    S["calibration_by_t"] = cal

    # 3. lead-lag profile (+lag: book moves after fair)
    res = meta["res"]
    nl = XC_MAX_S * 1000 // res
    for name, ds in (("all", days), ("holdout", hold)):
        xc = sum(D[x]["xc"] for x in ds)
        corr = xc[:, 0] / np.sqrt(xc[:, 1] * xc[:, 2])
        lags = np.arange(-nl, nl + 1) * res / 1000
        S[f"xcorr_{name}"] = {
            "peak_lag_s": float(lags[np.nanargmax(corr)]),
            "corr": {f"{lg:+.2f}": round(float(c), 4) for lg, c in zip(lags, corr) if abs(lg) <= 5},
        }

    # 4. gap closure: (mid_{t+h} - mid_t) = beta * (fair_t - mid_t), per-day slopes
    gc = {}
    for h in HS:
        sl = []
        for x in days:
            d = D[x]
            mid = (d["ub"] + d["ua"]) / 2
            g = ndtr(kk * d["z"]) - mid
            y = d[f"m{h}"] - mid
            k = np.isfinite(g) & np.isfinite(y)
            sl.append(float((g[k] @ y[k]) / (g[k] @ g[k])) if k.sum() > 100 else np.nan)
        sl = np.array(sl)
        gc[h] = boot(sl[np.isfinite(sl)])
    S["gap_closure_beta"] = gc

    # 5. taker trade test
    THETA = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)
    WINS = ((5, 296), (5, 120), (120, 240), (240, 296))
    EXS = ("settle",) + EXITS
    res_ = {}
    for L in LATS:
        for th in THETA:
            for win in WINS:
                per = {x: entries(D[x], kk, th, win, L, a.fee_rate) for x in days}
                for ex in EXS:
                    pd_ = {x: trade_pnl(D[x], per[x], L, ex, a.fee_rate) for x in days}
                    res_[(L, th, win, ex)] = pd_
    tab = []
    for (L, th, win, ex), pd_ in res_.items():
        r = {"lat_ms": L, "theta": th, "win": f"{win[0]}-{win[1]}", "exit": str(ex)}
        for name, ds in (("dev", dev), ("hold", hold)):
            daily = [SIZE * sum(pd_[x]) for x in ds]
            n = sum(len(pd_[x]) for x in ds)
            m_, lo, hi = boot(daily)
            r.update({f"{name}_n": n, f"{name}_pnl": round(sum(daily), 2),
                      f"{name}_c_per_sh": round(100 * sum(sum(pd_[x]) for x in ds) / max(n, 1), 2),
                      f"{name}_day_ci": [round(lo, 2), round(hi, 2)]})
        tab.append(r)
    T = pd.DataFrame(tab)
    T.to_csv(a.out / "trade_grid.csv", index=False)
    for L in LATS:
        g = T[(T.lat_ms == L) & (T.dev_n >= a.min_trades)].sort_values("dev_pnl", ascending=False)
        S[f"trades_lat{L}"] = {
            "dev_share_positive": round(float((T[T.lat_ms == L].dev_pnl > 0).mean()), 3),
            "hold_share_positive": round(float((T[T.lat_ms == L].hold_pnl > 0).mean()), 3),
            "selected_on_dev": g.head(1).to_dict("records"),
            "top5_dev": g.head(5).to_dict("records"),
        }
    S["prior_250ms_theta0.03_all_settle"] = T[
        (T.lat_ms == 250) & (T.theta == 0.03) & (T.win == "5-296") & (T.exit == "settle")
    ].to_dict("records")
    (a.out / "summary.json").write_text(json.dumps(S, indent=1, default=str))
    print(json.dumps(S, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("btc")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p.add_argument("--src", choices=tuple(SRC), default="spot1s")
    p.add_argument("--res", type=int, default=1000, help="grid ms")
    p.add_argument("--days", nargs="*")
    p.add_argument("--keep-zip", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("extract")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p.add_argument("--src", choices=tuple(SRC), default="spot1s")
    p.add_argument("--res", type=int, default=1000)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--vol-win", type=int, default=1800, help="trailing vol window, s")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("stats")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--holdout-days", type=int, default=10)
    p.add_argument("--fee-rate", type=float, default=0.07)
    p.add_argument("--min-trades", type=int, default=100)
    a = ap.parse_args()
    {"btc": cmd_btc, "extract": cmd_extract, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
