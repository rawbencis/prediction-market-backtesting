"""Does BTC spot add information beyond the Polymarket price on BTC 5m up/down markets?

fetch: Binance public 1s BTCUSDT spot klines (data.binance.vision) for the cached days.
snap:  every SNAP_S seconds per market: Polymarket UP mid (last valid quote), BTC features known
       at that instant (last COMPLETED 1s kline), winner.
eval:  rolling walk-forward logistic models refit per day on the previous TRAIN days:
         A = Polymarket price only, B = A + BTC features. Out-of-sample log-loss / Brier by time
       left, plus a diagnostic taker test (buy the side whose model prob exceeds ask + fee + theta).
"""

import argparse
import io
import json
import sys
import urllib.request
import zipfile
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import same_token_wf2 as wf

SNAP_S = range(10, 300, 10)
VOL_S = 300  # realized-vol lookback, s
MOM_S = (5, 15, 30, 60)
URL = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1s/BTCUSDT-1s-{m}.zip"


# ---------------------------------------------------------------- BTC series


def cmd_fetch(a):
    days = [d.name for d in wf.done_days(a.cache)]
    months = sorted({d[:7] for d in days})
    a.btc.mkdir(parents=True, exist_ok=True)
    for m in months:
        out = a.btc / f"BTCUSDT-1s-{m}.npz"
        if out.exists():
            continue
        req = urllib.request.Request(URL.format(m=m), headers={"User-Agent": "research-backtest"})
        print("downloading", m, flush=True)
        with urllib.request.urlopen(req, timeout=600) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, usecols=[0, 4])
        if not str(df.iloc[0, 0]).isdigit():  # header row in some files
            df = df.iloc[1:].astype({0: "int64", 4: "float64"})
        ts = df[0].to_numpy(np.int64)
        sec = np.where(ts > 10**14, ts // 1_000_000, ts // 1000)  # us (2025+) or ms
        np.savez_compressed(out, sec=sec, close=df[4].to_numpy(np.float64))
        print(m, len(sec), "seconds", flush=True)


class Btc:
    """Close of the 1s kline that OPENED at second s (known from s+1)."""

    def __init__(self, folder):
        parts = [np.load(f) for f in sorted(Path(folder).glob("BTCUSDT-1s-*.npz"))]
        if not parts:
            sys.exit(f"no BTC npz in {folder}; run fetch")
        sec = np.concatenate([p["sec"] for p in parts])
        close = np.concatenate([p["close"] for p in parts])
        o = np.argsort(sec)
        self.s0 = int(sec[o][0])
        n = int(sec[o][-1]) - self.s0 + 1
        self.px = np.full(n, np.nan)
        self.px[sec[o] - self.s0] = close[o]
        self.px = pd.Series(self.px).ffill().to_numpy()  # missing seconds: last close
        self.lr = np.r_[0.0, np.diff(np.log(self.px))]

    def known(self, t_us):
        """Index of the last kline completed by time t_us (opened <= t/1e6 - 1)."""
        return int(t_us // 1_000_000) - 1 - self.s0

    def features(self, epoch, t_us_abs):
        i, i0 = self.known(t_us_abs), self.known(epoch * 1_000_000)
        if i0 - VOL_S < 0 or i >= len(self.px) or i < i0:
            return None
        sig = max(np.std(self.lr[i - VOL_S + 1 : i + 1]), 1e-6)  # per-second log vol
        left = max(epoch + 300 - t_us_abs / 1e6, 1.0)
        d = np.log(self.px[i] / self.px[i0])
        f = {"z": d / (sig * np.sqrt(left)), "sig_bps": sig * 1e4}
        for h in MOM_S:
            f[f"m{h}"] = np.log(self.px[i] / self.px[i - h]) / (sig * np.sqrt(h))
        return f


# ---------------------------------------------------------------- snapshots


def snapshots(e, m, btc, winner):
    t = m["t"]
    if len(t) == 0:
        return []
    gs = (m["bid"] >= 0) & ((t[:, None] - m["seen"]) <= wf.AGE_US)
    use = np.flatnonzero(gs.all(1))
    rows = []
    for s in SNAP_S:
        k = np.searchsorted(t[use], s * 1_000_000, "right") - 1
        if k < 0 or s * 1_000_000 - t[use[k]] > wf.AGE_US:
            continue  # no fresh valid book at the snapshot
        i = use[k]
        f = btc.features(e, e * 1_000_000 + s * 1_000_000)
        if f is None:
            continue
        mid = (m["bid"][i, 0] + m["ask"][i, 0]) / 2000
        rows.append(
            {
                "epoch": e,
                "s": s,
                "mid": mid,
                "ask_up": m["ask"][i, 0] / 1000,
                "ask_dn": m["ask"][i, 1] / 1000,
                "up_won": int(winner == 0),
                **f,
            }
        )
    return rows


def _day(args):
    ddir, sett, btc_dir = args
    btc = Btc(btc_dir)
    rows = []
    for e, m in wf.markets(ddir):
        if e in sett:
            rows += snapshots(e, m, btc, sett[e])
    return ddir.name, [{**r, "day": ddir.name} for r in rows]


def cmd_snap(a):
    sett = wf.settlements(a.cache)
    out = []
    with Pool(a.workers) as pool:
        for d, r in pool.imap_unordered(_day, [(d, sett, a.btc) for d in wf.done_days(a.cache)]):
            out += r
            print(d, len(r), flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(out).sort_values(["epoch", "s"]).to_csv(a.out, index=False)
    print("wrote", a.out, len(out))


# ---------------------------------------------------------------- models


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def design(df, btc):
    tau = (300 - df["s"].to_numpy()) / 300
    lm = logit(df["mid"].to_numpy())
    cols = [np.ones(len(df)), lm, lm * tau]
    if btc:
        z = np.clip(df["z"].to_numpy(), -8, 8)
        cols += [z, z * tau] + [np.clip(df[f"m{h}"].to_numpy(), -8, 8) for h in MOM_S]
    return np.column_stack(cols)


def fit_logistic(X, y, ridge=1e-3, iters=50):
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w)))
        g = X.T @ (p - y) + ridge * w
        H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict(X, w):
    return 1 / (1 + np.exp(-(X @ w)))


def cmd_eval(a):
    df = pd.read_csv(a.snaps)
    days = sorted(df.day.unique())
    preds = []
    for i in range(a.train_days, len(days)):
        tr = df[df.day.isin(days[i - a.train_days : i])]
        te = df[df.day == days[i]].copy()
        y = tr["up_won"].to_numpy(float)
        for name, btc in (("A", False), ("B", True)):
            w = fit_logistic(design(tr, btc), y)
            te[f"p{name}"] = predict(design(te, btc), w)
        preds.append(te)
    p = pd.concat(preds)
    hold = days[-a.holdout_days]
    p["phase"] = np.where(p.day >= hold, "holdout", "dev")
    y = p["up_won"].to_numpy(float)

    def ll(q):
        q = np.clip(q, 1e-6, 1 - 1e-6)
        return -(y * np.log(q) + (1 - y) * np.log(1 - q))

    p["ll_mid"], p["llA"], p["llB"] = ll(p["mid"]), ll(p["pA"]), ll(p["pB"])
    p["br_mid"], p["brA"], p["brB"] = [(p[c] - y) ** 2 for c in ("mid", "pA", "pB")]
    p["bucket"] = pd.cut(
        p["s"],
        [0, 60, 120, 180, 240, 300],
        right=False,
        labels=["0-60", "60-120", "120-180", "180-240", "240-300"],
    )

    def gain_ci(g):
        # B vs A log-loss improvement per snapshot, CI by resampling days
        d = g.groupby("day").apply(lambda x: (x.llA - x.llB).sum()).to_numpy()
        n = g.groupby("day").size().to_numpy()
        rng = np.random.default_rng(0)
        b = [d[ix].sum() / n[ix].sum() for ix in rng.integers(0, len(d), (2000, len(d)))]
        return d.sum() / n.sum(), np.percentile(b, 5), np.percentile(b, 95)

    rows = []
    for (ph, bk), g in p.groupby(["phase", "bucket"], observed=True):
        gm, lo, hi = gain_ci(g)
        rows.append(
            {
                "phase": ph,
                "secs_in": bk,
                "n": len(g),
                "ll_mid": round(g.ll_mid.mean(), 4),
                "ll_A": round(g.llA.mean(), 4),
                "ll_B": round(g.llB.mean(), 4),
                "B_gain": round(gm, 4),
                "B_gain_ci90": [round(lo, 4), round(hi, 4)],
                "brier_mid": round(g.br_mid.mean(), 4),
                "brier_B": round(g.brB.mean(), 4),
            }
        )
    for ph, g in p.groupby("phase"):
        gm, lo, hi = gain_ci(g)
        rows.append(
            {
                "phase": ph,
                "secs_in": "ALL",
                "n": len(g),
                "ll_mid": round(g.ll_mid.mean(), 4),
                "ll_A": round(g.llA.mean(), 4),
                "ll_B": round(g.llB.mean(), 4),
                "B_gain": round(gm, 4),
                "B_gain_ci90": [round(lo, 4), round(hi, 4)],
                "brier_mid": round(g.br_mid.mean(), 4),
                "brier_B": round(g.brB.mean(), 4),
            }
        )
    t = pd.DataFrame(rows)
    print("== out-of-sample log-loss (lower is better); B_gain = A minus B per snapshot")
    print(t.to_string(index=False))

    # diagnostic taker test: buy UP if pB - ask_up - fee > theta, DOWN if (1-pB) - ask_dn - fee > theta
    print(
        f"\n== diagnostic taker test (one trade per market: first qualifying snapshot), "
        f"fee {a.fee_rate}*p*(1-p)"
    )
    out = []
    for th in (0.0, 0.02, 0.05, 0.08):
        for ph, g in p.groupby("phase"):
            fu = a.fee_rate * g.ask_up * (1 - g.ask_up)
            fd = a.fee_rate * g.ask_dn * (1 - g.ask_dn)
            eu, ed = g.pB - g.ask_up - fu, (1 - g.pB) - g.ask_dn - fd
            sig = g.assign(
                side=np.where(eu > th, "up", np.where(ed > th, "dn", "")),
                pnl=np.where(
                    eu > th,
                    g.up_won - g.ask_up - fu,
                    np.where(ed > th, 1 - g.up_won - g.ask_dn - fd, np.nan),
                ),
            )
            tr = sig[sig.side != ""].sort_values("s").drop_duplicates("epoch")
            daily = tr.groupby("day").pnl.sum().reindex(g.day.unique(), fill_value=0)
            b = np.random.default_rng(0).choice(daily.to_numpy(), (2000, len(daily))).mean(1)
            out.append(
                {
                    "theta": th,
                    "phase": ph,
                    "trades": len(tr),
                    "pnl_per_trade": round(tr.pnl.mean(), 4) if len(tr) else 0,
                    "pnl_per_day": round(daily.mean(), 3),
                    "ci90_day": [round(np.percentile(b, 5), 3), round(np.percentile(b, 95), 3)],
                }
            )
    print(pd.DataFrame(out).to_string(index=False))
    p.to_csv(a.snaps.with_name("overlay_oos_predictions.csv"), index=False)
    (a.snaps.with_name("overlay_eval.json")).write_text(
        json.dumps({"loglik": rows, "taker": out}, indent=1, default=str)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("fetch")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p = sp.add_parser("snap")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p = sp.add_parser("eval")
    p.add_argument("--snaps", type=Path, required=True)
    p.add_argument("--train-days", type=int, default=14)
    p.add_argument("--holdout-days", type=int, default=10)
    p.add_argument("--fee-rate", type=float, default=0.07)
    a = ap.parse_args()
    {"fetch": cmd_fetch, "snap": cmd_snap, "eval": cmd_eval}[a.cmd](a)


if __name__ == "__main__":
    main()
