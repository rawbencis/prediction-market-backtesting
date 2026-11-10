"""Cross-asset opposite-direction pairs on Polymarket 5m up/down (BTC/ETH/SOL/XRP...).

Idea: buy A_UP + B_DOWN (or A_DOWN + B_UP) for less than $1; correlated assets mostly finish on the
same side, so the pair mostly pays exactly $1. Payoff: 2 if A up & B down, 1 if same side, 0 if A
down & B up. E[payoff] = P(A up) + P(B down): correlation changes the variance, not the mean. A cost
below $1 only appears after A has fallen behind B, i.e. the pair already sits in the payoff-0 corner.
So the edge, if any, is (a) intra-window catch-up beyond what each asset's own state implies and
(b) Polymarket not pricing it. Both are tested, plus the naive rule exactly as stated.

Stages (each independent, fail-soft):
  inv     what quote parquet exists (assets, dates, dirs), disk
  klines  Binance 1s spot klines -> per-second close arrays (underlying proxy; settlement is Chainlink)
  settle  Gamma winners for non-BTC assets on the BTC epochs
  grid    per asset 250 ms top-of-book grid (fresh + mirrored books only)
  pairs   per pair/day: 10 s snapshots, cost histogram, taker trigger grid with leg risk and exits
  report  S1 outcome dependence, S2 underlying catch-up model, S3-S6 market tests
"""

import argparse
import csv
import io
import json
import shutil
import sys
import traceback
import urllib.request
import zipfile
from collections import Counter
from itertools import combinations
from multiprocessing import Pool
from pathlib import Path

import numpy as np

import same_token_wf2 as wf

FEE = 0.07  # crypto taker fee: 0.07*p*(1-p) per share; maker 0
SIZE = wf.SIZE
STEP = 250_000  # grid step == assumed latency
G = np.arange(0, wf.MKT_US, STEP)
NG = len(G)
GS = G / 1e6
SNAP_S = tuple(range(10, 300, 10))
SNAP_I = np.array([s * 1_000_000 // STEP for s in SNAP_S])
M = (0, 20, 50, 80, 100, 150, 200, 300)  # required discount to $1 after fees, milli
WIN = ((5, 290), (5, 120), (120, 240), (240, 290))  # entry window, s
TGT = (30, 60, 100)  # convergence exit target, milli (plus hold-to-settlement)
CFG = [(m, w) for m in M for w in range(len(WIN))]
VAR = ("hold",) + tuple(f"x{t}" for t in TGT)
PRE = CFG.index((50, 0))  # pre-registered: 5c net discount, any time 5-290 s, hold
# two-legged MAKER pair (S7): post both bids when the pair's MID cost <= 1000-m; fee 0, rebates ignored
MK_M = (50, 100)
MK_WIN = ((10, 90), (5, 290))
MK_TTL = (10, 30, 60)
MK_CFG = [(m, w, ttl, imp, fm) for m in MK_M for w in range(len(MK_WIN)) for ttl in MK_TTL for imp in (0, 10) for fm in (0, 1)]
MK_PRE = MK_CFG.index((50, 0, 30, 0, 0))  # pre-registered: mid cost<=950, 10-90 s, ttl 30, no improve, trade-through
HOLDOUT = "2026-07-19"
INCIDENT = ("2026-06-01", "2026-06-02", "2026-06-03")  # desynced-quote burst seen in pair_gap_scan
DEC = np.array([30, 60, 90, 120, 150, 180, 210, 240, 270])  # underlying decision times, s
VOL_S = 3600
CB = (0, 700, 800, 850, 900, 950, 980, 1000, 1020, 1050, 1100, 1200, 1300, 2001)  # pair cost buckets
DB = (-np.inf, -2, -1, -0.5, 0.5, 1, 2, np.inf)  # divergence z_other - z_own
RNG = np.random.default_rng(0)


def fee(p):
    return FEE * p * (1000 - p) / 1000  # milli/share, p in milli


def sig(x):
    return 1 / (1 + np.exp(-np.clip(x, -30, 30)))


def ncdf(z):
    """Normal CDF (Abramowitz-Stegun 7.1.26, |err| < 2e-7); numpy has no erf."""
    x = np.abs(z) / np.sqrt(2)
    t = 1 / (1 + 0.3275911 * x)
    e = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-x * x)
    return 0.5 * (1 + np.sign(z) * e)


def lgt(p, eps=1e-4):
    p = np.clip(p, eps, 1 - eps)
    return np.log(p / (1 - p))


def own_X(L, tau=None):
    """Flexible own-state design so that own-model misfit is not credited to the other asset."""
    cols = [np.ones_like(L), L, L * np.abs(L)]
    if tau is not None:
        cols.append(L * tau)
    return np.column_stack(cols)


def bin_resid(y, x, t, nb=50):
    """y minus the mean of y within (quantile bin of x, t): non-parametric own-state residual."""
    q = np.searchsorted(np.quantile(x, np.linspace(0, 1, nb + 1)[1:-1]), x)
    _, key = np.unique(q * 100000 + t, return_inverse=True)
    return y - (np.bincount(key, y) / np.bincount(key))[key]


def logit_fit(X, y, l2=1e-6, it=30):
    b = np.zeros(X.shape[1])
    for _ in range(it):
        p = sig(X @ b)
        H = X.T @ (X * (p * (1 - p) + 1e-9)[:, None]) + l2 * np.eye(len(b))
        d = np.linalg.solve(H, X.T @ (y - p) - l2 * b)
        b += d
        if np.abs(d).max() < 1e-8:
            break
    return b


def cluster_se(X, y, b, day):
    p = sig(X @ b)
    Hi = np.linalg.inv(X.T @ (X * (p * (1 - p) + 1e-9)[:, None]))
    _, inv = np.unique(day, return_inverse=True)
    Gd = np.zeros((inv.max() + 1, X.shape[1]))
    np.add.at(Gd, inv, X * (y - p)[:, None])
    return np.sqrt(np.diag(Hi @ (Gd.T @ Gd) @ Hi))


def wf_prob(day, Xs, y, min_train=14, step=1):
    """Expanding walk-forward by day (refit every `step` days) -> out-of-sample probabilities per design."""
    days = np.unique(day)
    P = [np.full(len(y), np.nan) for _ in Xs]
    for k in range(min_train, len(days), step):
        tr = day < days[k]
        te = (day >= days[k]) & (day <= days[min(k + step, len(days)) - 1])
        for i, X in enumerate(Xs):
            P[i][te] = sig(X[te] @ logit_fit(X[tr], y[tr]))
    return P


def boot_mean(v, day, B=1000):
    """Mean of v with a day-cluster bootstrap 90% CI."""
    v = np.asarray(v, float)
    if not len(v):
        return np.nan, np.nan, np.nan
    _, inv = np.unique(day, return_inverse=True)
    s = np.bincount(inv, v)
    c = np.bincount(inv)
    i = RNG.integers(0, len(s), (B, len(s)))
    m = s[i].sum(1) / np.maximum(c[i].sum(1), 1)
    return float(v.mean()), float(np.percentile(m, 5)), float(np.percentile(m, 95))


def boot_sum(daily, B=2000):
    x = np.asarray(daily, float)
    if len(x) < 2:
        return float(x.sum()), np.nan, np.nan
    s = x[RNG.integers(0, len(x), (B, len(x)))].sum(1)
    return float(x.sum()), float(np.percentile(s, 5)), float(np.percentile(s, 95))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


# ---------------------------------------------------------------- inv


def cmd_inv(a):
    a.out.mkdir(parents=True, exist_ok=True)
    inv = {}
    for root in a.data:
        root = Path(root)
        print(f"[inv] {root}: {sorted(p.name for p in root.iterdir())[:40] if root.is_dir() else 'MISSING'}")
        if root.parent.is_dir():
            print(f"[inv] {root.parent}: {sorted(p.name for p in root.parent.iterdir())[:60]}")
        if not root.is_dir():
            continue
        for f in root.rglob("*-updown-*_UP.parquet"):
            pre, ep = f.name[: -len("_UP.parquet")].rsplit("-", 1)
            if not ep.isdigit():
                continue
            d = inv.setdefault(pre, {"dirs": Counter(), "n": 0, "lo": 1 << 60, "hi": 0, "bytes": 0})
            d["dirs"][str(f.parent)] += 1
            d["n"] += 1
            d["lo"], d["hi"] = min(d["lo"], int(ep)), max(d["hi"], int(ep))
            d["bytes"] += f.stat().st_size
    res = {}
    for pre, d in sorted(inv.items()):
        res[pre] = dict(dir=d["dirs"].most_common(1)[0][0], n=d["n"], first=wf.day_of(d["lo"]), last=wf.day_of(d["hi"]))
        print(f"[inv] {pre:>18}: {d['n']:>6} markets {res[pre]['first']}..{res[pre]['last']} "
              f"{d['bytes'] / 1e9:.1f} GB (UP files) dirs={dict(d['dirs'].most_common(3))}")
    if not res:
        print("[inv] no *-updown-*_UP.parquet found")
    if a.btc_cache:
        print(f"[inv] btc npz cache {a.btc_cache}: {len(wf.done_days(a.btc_cache))} days, "
              f"{len(wf.settlements(a.btc_cache))} winners")
    du = shutil.disk_usage(a.out)
    print(f"[inv] disk free {du.free / 1e9:.0f} GB of {du.total / 1e9:.0f} GB")
    (a.out / "inv.json").write_text(json.dumps(res, indent=1))


# ---------------------------------------------------------------- klines


def parse_klines(raw):
    """Binance kline CSV bytes -> (second int64[], close float64[]). Handles header and ms/us stamps."""
    skip = 0 if raw[:1].isdigit() else 1
    x = np.loadtxt(io.BytesIO(raw), delimiter=",", usecols=(0, 4), skiprows=skip, dtype=np.float64, ndmin=2)
    ts = x[:, 0]
    unit = 1e6 if ts[0] > 1e14 else 1e3 if ts[0] > 1e11 else 1.0
    return (ts // unit).astype(np.int64), x[:, 1]


def dense(sec, close):
    """-> t0, per-second close with gaps forward-filled."""
    o = np.argsort(sec, kind="stable")
    sec, close = sec[o], close[o]
    t0 = int(sec[0])
    out = np.full(int(sec[-1]) - t0 + 1, np.nan)
    out[sec - t0] = close
    idx = np.where(np.isfinite(out), np.arange(len(out)), 0)
    np.maximum.accumulate(idx, out=idx)
    return t0, out[idx], int(np.isnan(out).sum())


def months(start, end):
    y, m = map(int, start.split("-"))
    ye, me = map(int, end.split("-"))
    while (y, m) <= (ye, me):
        yield f"{y}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def cmd_klines(a):
    kd = a.out / "kl"
    (kd / "raw").mkdir(parents=True, exist_ok=True)
    bad = 0
    for asset in a.assets:
        sym = asset.upper() + "USDT"
        secs, cls = [], []
        for mo in months(a.start, a.end):
            f = kd / "raw" / f"{sym}-1s-{mo}.zip"
            if not f.exists():
                url = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/1s/{f.name}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "research-backtest"})
                    if bad >= 2 and not any(kd.glob("raw/*.zip")):
                        raise OSError("skipped: data.binance.vision unreachable")
                    with urllib.request.urlopen(req, timeout=60) as r, f.with_suffix(".tmp").open("wb") as o:
                        shutil.copyfileobj(r, o)
                    f.with_suffix(".tmp").replace(f)
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    print(f"[klines] {url} failed: {e!r}", flush=True)
                    continue
            with zipfile.ZipFile(f) as z:
                s, c = parse_klines(z.read(z.namelist()[0]))
            secs.append(s)
            cls.append(c)
            print(f"[klines] {f.name}: {len(s)} rows", flush=True)
        if not secs:
            continue
        t0, close, gaps = dense(np.concatenate(secs), np.concatenate(cls))
        np.savez(kd / f"{asset}.npz", t0=t0, close=close)
        print(f"[klines] {asset}: {wf.day_of(t0)}..{wf.day_of(t0 + len(close))} filled gaps {gaps} s", flush=True)


def load_px(out, assets):
    d = {}
    for a in assets:
        f = out / "kl" / f"{a}.npz"
        if f.exists():
            z = np.load(f)
            d[a] = (int(z["t0"]), z["close"])
    return d


# ---------------------------------------------------------------- settle


def _gamma(slugs):
    url = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(f"slug={s}" for s in slugs)
    req = urllib.request.Request(url, headers={"User-Agent": "research-backtest"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_winners(rows, want):
    new = {}
    for m in rows:
        try:
            names = [n.upper() for n in json.loads(m["outcomes"])]
            px = [float(v) for v in json.loads(m["outcomePrices"])]
        except (KeyError, TypeError, ValueError):
            continue
        if m.get("slug") in want and m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(px) >= 0.99 and min(px) <= 0.01:
            new[m["slug"]] = names[px.index(max(px))]
    return new


def fetch_winners(asset, epochs, get=_gamma, log=print):
    new, fails = {}, 0
    slugs = [f"{asset}-updown-5m-{e}" for e in epochs]
    for i in range(0, len(slugs), 50):
        batch = slugs[i : i + 50]
        try:
            got = parse_winners(get(batch), set(batch))
            for sl in [x for x in batch if x not in got] if got else []:
                got.update(parse_winners(get([sl]), {sl}))
            new.update(got)
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"[settle] {asset} batch {i} failed: {e!r}")
        if (i == 100 or fails >= 3) and not new:
            log(f"[settle] {asset}: nothing resolved after {i + len(batch)} slugs ({fails} failed requests), skipping asset")
            break
        if (i // 50) % 40 == 0:
            log(f"[settle] {asset} {i + len(batch)}/{len(slugs)} resolved={len(new)} failed_batches={fails}")
    return {int(s.rsplit("-", 1)[1]): w for s, w in new.items()}


def read_winners(p):
    if not p.exists():
        return {}
    with p.open() as f:
        return {int(r["epoch"]): r["winner"] for r in csv.DictReader(f)}


def cmd_settle(a):
    a.out.mkdir(parents=True, exist_ok=True)
    epochs = sorted(wf.settlements(a.btc_cache))
    for asset in a.assets:
        p = a.out / f"settle_{asset}.csv"
        have = read_winners(p)
        have.update(fetch_winners(asset, [e for e in epochs if e not in have]))
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["epoch", "winner"])
            w.writerows(sorted(have.items()))
        print(f"[settle] {asset}: {len(have)}/{len(epochs)} winners -> {p}", flush=True)


def winners_up(out, btc_cache, asset):
    """-> {epoch: 1 if UP won else 0}"""
    if asset == "btc" and btc_cache:
        return {e: 1 - s for e, s in wf.settlements(btc_cache).items()}
    return {e: int(w == "UP") for e, w in read_winners(out / f"settle_{asset}.csv").items()}


# ---------------------------------------------------------------- grid


GK = ("bU", "aU", "szU", "szD", "bzU", "bzD", "v", "vm")


def to_grid(m, age=wf.AGE_US):
    """wf2 market dict -> 250 ms grid of the UP book (DOWN is the mirror when v)."""
    t = m["t"]
    z16 = np.zeros(NG, np.int16)
    if not len(t):
        return dict(bU=z16, aU=z16, szU=z16, szD=z16, bzU=z16, bzD=z16, v=np.zeros(NG, bool), vm=np.zeros(NG, bool))
    i = np.searchsorted(t, G, "right") - 1
    ii = np.maximum(i, 0)
    bid, ask = m["bid"][ii].astype(np.int32), m["ask"][ii].astype(np.int32)
    vm = (i >= 0) & (bid >= 0).all(1) & ((G[:, None] - m["seen"][ii]) <= age).all(1)
    v = vm & (ask[:, 1] == 1000 - bid[:, 0]) & (ask[:, 0] == 1000 - bid[:, 1])

    def sz(x):
        return np.minimum(x[ii], 30000).astype(np.int16)

    return dict(
        bU=bid[:, 0].astype(np.int16), aU=ask[:, 0].astype(np.int16), szU=sz(m["asz"][:, 0]), szD=sz(m["asz"][:, 1]),
        bzU=sz(m["bsz"][:, 0]), bzD=sz(m["bsz"][:, 1]), v=v, vm=vm,
    )


def _grid_day(args):
    asset, day, kind, src, epochs, out = args
    it = wf.markets(Path(src) / day) if kind == "npz" else ((e, wf.load_market((Path(src), e))[1]) for e in epochs)
    es, rows = [], []
    for e, m in it:
        if m is not None:
            es.append(e)
            rows.append(to_grid(m))
    if es:
        tmp = out / f".{day}.tmp"
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, epochs=np.array(es), **{k: np.stack([r[k] for r in rows]) for k in GK})
        tmp.replace(out / f"{day}.npz")
    return asset, day, len(es)


def cmd_grid(a):
    inv = json.loads((a.out / "inv.json").read_text()) if (a.out / "inv.json").exists() else {}
    jobs = []
    for asset in a.assets:
        out = a.out / "grid" / asset
        out.mkdir(parents=True, exist_ok=True)
        src = dict(s.split("=", 1) for s in a.npz).get(asset)
        if src:
            for d in wf.done_days(Path(src)):
                jobs.append((asset, d.name, "npz", src, None, out))
            continue
        pre = f"{asset}-updown-5m"
        if pre not in inv:
            print(f"[grid] {asset}: no quote data (no npz cache given, no {pre}-*.parquet in inv.json)")
            continue
        links = a.out / "links" / asset  # wf.load_market hardcodes the btc file prefix
        links.mkdir(parents=True, exist_ok=True)
        days = {}
        for f in Path(inv[pre]["dir"]).glob(f"{pre}-*_UP.parquet"):
            e = int(f.name[: -len("_UP.parquet")].rsplit("-", 1)[1])
            for side in wf.SIDES:
                ln = links / f"btc-updown-5m-{e}_{side}.parquet"
                if not ln.exists():
                    ln.symlink_to(f.with_name(f"{pre}-{e}_{side}.parquet"))
            days.setdefault(wf.day_of(e), []).append(e)
        for d, es in days.items():
            jobs.append((asset, d, "parquet", str(links), sorted(es), out))
    jobs = [j for j in jobs if a.force or not (j[5] / f"{j[1]}.npz").exists()]
    if a.days:
        keep = sorted({j[1] for j in jobs})[: a.days]
        jobs = [j for j in jobs if j[1] in keep]
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for asset, d, n in pool.imap_unordered(_grid_day, jobs):
            print(f"[grid] {asset} {d} {n}", flush=True)


# ---------------------------------------------------------------- pairs


def take(X, i):
    return np.take_along_axis(X, i[:, None], 1)[:, 0]


def pair_day(ga, gb, Wa, Wb):
    """ga/gb: grid dicts aligned by market (n, NG); Wa/Wb: 1 if UP won.

    orientation 0 = A_UP + B_DOWN, 1 = A_DOWN + B_UP.
    -> snap int16 (n, len(SNAP), 9), hist float (len(WIN)+1, len(M)+1), trig float32 (n, len(CFG), 6+len(TGT))
    trig cols: code (-1 no trigger, 0 none filled, 1 both, 2 A leg only, 3 B leg only), orientation,
    trigger time s, net cost paid, payoff (0/1/2), pnl hold, pnl with exit target... (milli/share, orphans incl.)
    """
    I = lambda k, g: g[k].astype(np.int32)  # noqa: E731
    n = len(Wa)
    v = ga["v"] & gb["v"]
    askA, askB = [I("aU", ga), 1000 - I("bU", ga)], [1000 - I("bU", gb), I("aU", gb)]
    bidA, bidB = [I("bU", ga), 1000 - I("aU", ga)], [1000 - I("aU", gb), I("bU", gb)]
    szA, szB = [I("szU", ga), I("szD", ga)], [I("szD", gb), I("szU", gb)]
    bzA, bzB = [I("bzU", ga), I("bzD", ga)], [I("bzD", gb), I("bzU", gb)]
    wA, wB = [Wa, 1 - Wa], [1 - Wb, Wb]
    net = [askA[o] + askB[o] + fee(askA[o]) + fee(askB[o]) for o in (0, 1)]
    big = [(np.minimum(szA[o], szB[o]) >= SIZE) for o in (0, 1)]

    snap = np.stack(
        [x[:, SNAP_I] for x in (v, I("bU", ga), I("aU", ga), I("bU", gb), I("aU", gb), szA[0], szA[1], szB[1], szB[0])], 2
    ).astype(np.int16)  # v, bA, aA, bB, aB, szAU, szAD, szBU, szBD

    mn = np.where(v, np.minimum(net[0], net[1]), np.inf)
    hist = np.zeros((len(WIN) + 1, len(M) + 1))
    for wi, (w0, w1) in enumerate(WIN + ((0, 300),)):
        inw = (GS >= w0) & (GS < w1)
        hist[wi, 0] = v[:, inw].sum()
        hist[wi, 1:] = [(mn[:, inw] <= 1000 - m).sum() for m in M]

    trig = np.zeros((n, len(CFG), 6 + len(TGT)), np.float32)
    ar = np.arange(NG)[None, :]
    for ci, (m, wi) in enumerate(CFG):
        inw = (GS >= WIN[wi][0]) & (GS < WIN[wi][1])
        ok = [v & inw & (net[o] <= 1000 - m) & big[o] for o in (0, 1)]
        i_o = [np.where(ok[o].any(1), ok[o].argmax(1), NG) for o in (0, 1)]
        i = np.minimum(i_o[0], i_o[1])
        ic = np.minimum(i, NG - 1)
        o = (i_o[1] < i_o[0]) | ((i_o[1] == i_o[0]) & (take(net[1], ic) < take(net[0], ic)))
        has = i < NG - 1
        j = np.minimum(ic + 1, NG - 1)
        S = lambda P: np.where(o[:, None], P[1], P[0])  # noqa: E731
        aA, aB, sA, sB = S(askA), S(askB), S(szA), S(szB)
        pA, pB = take(aA, j), take(aB, j)
        fA = has & take(ga["v"], j) & (pA <= take(aA, ic)) & (take(sA, j) >= SIZE)
        fB = has & take(gb["v"], j) & (pB <= take(aB, ic)) & (take(sB, j) >= SIZE)
        code = np.where(~has, -1, np.where(fA & fB, 1, np.where(fA, 2, np.where(fB, 3, 0))))
        wa, wb = np.where(o, wA[1], wA[0]), np.where(o, wB[1], wB[0])
        cA, cB = pA + fee(pA), pB + fee(pB)
        cost = cA + cB
        hold = np.where(code == 1, 1000 * (wa + wb) - cost, np.where(code == 2, 1000 * wa - cA, np.where(code == 3, 1000 * wb - cB, 0.0)))
        trig[:, ci, 0], trig[:, ci, 1], trig[:, ci, 2] = code, o, np.where(has, GS[ic], np.nan)
        trig[:, ci, 3], trig[:, ci, 4], trig[:, ci, 5] = np.where(code == 1, cost, np.nan), np.where(code == 1, wa + wb, np.nan), hold
        bA, bB = S(bidA), S(bidB)
        bnet = bA + bB - fee(bA) - fee(bB)
        liq = v & (np.minimum(S(bzA), S(bzB)) >= SIZE)
        for ti, tg in enumerate(TGT):
            okx = liq & (ar > j[:, None]) & (bnet >= (cost + tg)[:, None])
            k = np.where(okx.any(1), okx.argmax(1), NG)
            k1 = np.minimum(k + 1, NG - 1)
            ex = (code == 1) & (k < NG - 1) & take(liq, k1)
            trig[:, ci, 6 + ti] = np.where(ex, take(bnet, k1) - cost, hold)
    mk = maker_pair(ga, gb, v, (bidA, askA, szA), (bidB, askB, szB), wA, wB)
    return snap, hist, trig, mk


def maker_pair(ga, gb, v, LA, LB, wA, wB):
    """-> float32 (n, len(MK_CFG), 5): code (-1 none, 0 no fill, 1 both, 2 A only, 3 B only), mid gap at
    trigger, pnl holding any orphan to settlement, pnl completing the orphan as taker at expiry, trigger s."""
    (bidA, askA, szA), (bidB, askB, szB) = LA, LB
    n = v.shape[0]
    midc = [(bidA[o] + askA[o] + bidB[o] + askB[o]) / 2 for o in (0, 1)]
    ar = np.arange(NG)[None, :]
    out = np.zeros((n, len(MK_CFG), 5), np.float32)
    for ci, (m, wi, ttl, imp, fm) in enumerate(MK_CFG):
        inw = (GS >= MK_WIN[wi][0]) & (GS < MK_WIN[wi][1])
        ok = [v & inw & (midc[o] <= 1000 - m) for o in (0, 1)]
        i_o = [np.where(ok[o].any(1), ok[o].argmax(1), NG) for o in (0, 1)]
        i = np.minimum(i_o[0], i_o[1])
        ic = np.minimum(i, NG - 1)
        o = (i_o[1] < i_o[0]) | ((i_o[1] == i_o[0]) & (take(midc[1], ic) < take(midc[0], ic)))
        j = np.minimum(ic + 1, NG - 1)
        okj = (i < NG - 1) & take(v, j)
        S = lambda P: np.where(o[:, None], P[1], P[0])  # noqa: E731
        bA, aA, sA, bB, aB, sB = S(bidA), S(askA), S(szA), S(bidB), S(askB), S(szB)
        px = []
        for b_, a_ in ((bA, aA), (bB, aB)):
            p = take(b_, j) + imp
            px.append(np.where(p < take(a_, j), p, take(b_, j)))
        hi = np.minimum(j + ttl * 1_000_000 // STEP, NG - 1)
        win = (ar > j[:, None]) & (ar <= hi[:, None])
        f = []
        for a_, s_, g, p in ((aA, sA, ga, px[0]), (aB, sB, gb, px[1])):
            hit = (a_ <= p[:, None]) if fm else (a_ < p[:, None])
            q = win & g["v"] & hit & (s_ >= SIZE)
            f.append(q.any(1) & okj)
        both, onlyA, onlyB = f[0] & f[1], f[0] & ~f[1], f[1] & ~f[0]
        wa, wb = np.where(o, wA[1], wA[0]), np.where(o, wB[1], wB[0])
        pay = 1000.0 * (wa + wb)
        hold = np.where(both, pay - px[0] - px[1], np.where(onlyA, 1000.0 * wa - px[0], np.where(onlyB, 1000.0 * wb - px[1], 0.0)))
        e = np.minimum(hi + 1, NG - 1)
        cA, cB = take(aA, e), take(aB, e)
        okA = take(ga["v"], e) & (take(sA, e) >= SIZE)
        okB = take(gb["v"], e) & (take(sB, e) >= SIZE)
        comp = np.where(onlyA & okB, pay - px[0] - cB - fee(cB), np.where(onlyB & okA, pay - px[1] - cA - fee(cA), hold))
        out[:, ci, 0] = np.where(~okj, -1, np.where(both, 1, np.where(onlyA, 2, np.where(onlyB, 3, 0))))
        out[:, ci, 1] = np.where(i < NG, 1000 - take(S(midc), ic), np.nan)
        out[:, ci, 2], out[:, ci, 3], out[:, ci, 4] = hold, comp, np.where(i < NG, GS[ic], np.nan)
    return out


def load_grid(out, asset, day):
    f = out / "grid" / asset / f"{day}.npz"
    if not f.exists():
        return None
    with np.load(f) as z:
        return {k: z[k] for k in ("epochs",) + GK}


def _pair_day(args):
    out, A, B, day, WA, WB = args
    ga, gb = load_grid(out, A, day), load_grid(out, B, day)
    if ga is None or gb is None:
        return A, B, day, 0
    ep = np.array([e for e in np.intersect1d(ga["epochs"], gb["epochs"]) if e in WA and e in WB], np.int64)
    if not len(ep):
        return A, B, day, 0
    oa, ob = np.argsort(ga["epochs"]), np.argsort(gb["epochs"])
    ia, ib = oa[np.searchsorted(ga["epochs"][oa], ep)], ob[np.searchsorted(gb["epochs"][ob], ep)]
    ga, gb = {k: ga[k][ia] for k in GK}, {k: gb[k][ib] for k in GK}
    Wa, Wb = np.array([WA[e] for e in ep]), np.array([WB[e] for e in ep])
    snap, hist, trig, mk = pair_day(ga, gb, Wa, Wb)
    d = out / "pairs" / f"{A}-{B}"
    d.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(d / f"{day}.npz", epochs=ep, Wa=Wa, Wb=Wb, snap=snap, hist=hist, trig=trig, mk=mk,
                        va=np.array([ga["v"].mean(), ga["vm"].mean()]), vb=np.array([gb["v"].mean(), gb["vm"].mean()]))
    return A, B, day, len(ep)


def grid_assets(out):
    return sorted(p.name for p in (out / "grid").iterdir() if any(p.glob("*.npz"))) if (out / "grid").is_dir() else []


def cmd_pairs(a):
    assets = grid_assets(a.out)
    W = {x: winners_up(a.out, a.btc_cache, x) for x in assets}
    assets = [x for x in assets if W[x]]
    print(f"[pairs] assets with grid + winners: {assets}")
    jobs = []
    for A, B in combinations(assets, 2):
        da = {f.stem for f in (a.out / "grid" / A).glob("*.npz")} & {f.stem for f in (a.out / "grid" / B).glob("*.npz")}
        jobs += [(a.out, A, B, d, W[A], W[B]) for d in sorted(da)]
    with Pool(a.workers) as pool:
        for A, B, d, n in pool.imap_unordered(_pair_day, jobs, chunksize=1):
            print(f"[pairs] {A}-{B} {d} {n}", flush=True)


# ---------------------------------------------------------------- report: outcomes + underlying


def joint(ua, ub):
    """outcome arrays (1 = up) -> dict"""
    n = len(ua)
    p11, p00 = np.mean(ua & ub), np.mean(~ua & ~ub)
    pa, pb = ua.mean(), ub.mean()
    phi = (p11 - pa * pb) / max(np.sqrt(pa * (1 - pa) * pb * (1 - pb)), 1e-12)
    return dict(n=n, pA=pa, pB=pb, same=p11 + p00, a_up_b_down=np.mean(ua & ~ub), a_down_b_up=np.mean(~ua & ub), phi=phi)


def rep_outcomes(a, R, P):
    P("\n== S1 outcome dependence (actual Polymarket settlements) ==")
    W = {x: winners_up(a.out, a.btc_cache, x) for x in a.assets}
    W = {x: w for x, w in W.items() if w}
    if len(W) < 2:
        P("  need winners for >= 2 assets (run settle)")
        return
    R["outcomes"] = {}
    for A, B in combinations(sorted(W), 2):
        ep = np.array(sorted(set(W[A]) & set(W[B])))
        if len(ep) < 100:
            continue
        ua, ub = np.array([W[A][e] for e in ep], bool), np.array([W[B][e] for e in ep], bool)
        j = joint(ua, ub)
        day = ep // 86400
        ds = np.array([np.mean(ua[day == d] == ub[day == d]) for d in np.unique(day) if (day == d).sum() >= 100])
        j["daily_same_min_p10_med_p90_max"] = [float(np.percentile(ds, q)) for q in (0, 10, 50, 90, 100)] if len(ds) else []
        R["outcomes"][f"{A}-{B}"] = j
        P(f"  {A}-{B}: n={j['n']} P(same)={j['same']:.3f} phi={j['phi']:.3f} P(A up,B down)={j['a_up_b_down']:.3f} "
          f"P(A down,B up)={j['a_down_b_up']:.3f} | P(up) {j['pA']:.3f}/{j['pB']:.3f} | daily P(same) min/p10/med/p90/max "
          f"{np.round(j['daily_same_min_p10_med_p90_max'], 3)}")
    P("  entering both legs at the open (~50c each, cost ~$1+spread): payoff 2/1/0 with the probabilities above; EV = P(A up)+P(B down)-cost")


def under_data(px, assets):
    """-> E (window epochs), per asset dict(z (n, len(DEC)), y, r5, sig)"""
    lo = max(px[x][0] for x in assets) + VOL_S + 2
    hi = min(px[x][0] + len(px[x][1]) for x in assets) - 301
    E = np.arange(-(-lo // 300) * 300, hi, 300)
    D = {}
    for x in assets:
        t0, c = px[x]
        lc = np.log(c)
        cs = np.r_[0, np.cumsum(np.diff(lc) ** 2)]
        s = np.sqrt(np.maximum(cs[E - t0 - 1] - cs[E - VOL_S - t0 - 1], 1e-16) / VOL_S)
        p0 = lc[E - t0 - 1]
        xs = np.stack([lc[E + t - t0 - 1] - p0 for t in DEC], 1)
        r5 = lc[E + 300 - t0 - 1] - p0
        D[x] = dict(z=np.clip(xs / (s[:, None] * np.sqrt(300 - DEC)), -6, 6), y=r5 >= 0, r5=r5, sig=s)
    return E, D


def catchup(zo, zx, y, p0, rb, day, P, label):
    """Does the other asset's state move the outcome beyond own-state pricing? rows by divergence bucket."""
    d = zx - zo
    rows = []
    for lo, hi in zip(DB[:-1], DB[1:]):
        s = (d >= lo) & (d < hi) & np.isfinite(p0)
        if s.sum() < 200:
            continue
        m, l, h = boot_mean(1000 * (y[s] - p0[s]), day[s])
        s2 = (d >= lo) & (d < hi)
        m2, l2, h2 = boot_mean(1000 * rb[s2], day[s2])
        rows.append(dict(d_lo=float(lo), d_hi=float(hi), n=int(s.sum()), resid_milli=m, ci90=[l, h], resid_binned=m2, ci90_binned=[l2, h2]))
        P(f"      {label} z_other-z_own in [{lo:>4},{hi:>4}): n={s.sum():>7} realised - own-state model = {m:+6.1f} milli [{l:+.1f},{h:+.1f}] "
          f"| vs binned own state {m2:+6.1f} [{l2:+.1f},{h2:+.1f}]")
    return rows


def rep_under(a, R, P):
    P("\n== S2 underlying (Binance 1s spot as proxy; settlement itself is Chainlink) ==")
    px = load_px(a.out, a.assets)
    if len(px) < 2:
        P("  need klines for >= 2 assets (run klines)")
        return
    assets = sorted(px)
    E, D = under_data(px, assets)
    day = E // 86400
    R["under"] = dict(windows=len(E), first=wf.day_of(int(E[0])), last=wf.day_of(int(E[-1])), pairs={}, proxy={})
    P(f"  {len(E)} windows {R['under']['first']}..{R['under']['last']}; vol = trailing {VOL_S}s realised; ties (close==open) count as Up")
    for x in assets:
        W = winners_up(a.out, a.btc_cache, x)
        k = np.array([e in W for e in E])
        if k.sum() > 100:
            act = np.array([W[e] for e in E[k]], bool)
            ag = float(np.mean(act == D[x]["y"][k]))
            sm = np.abs(D[x]["r5"][k]) < 1e-4
            R["under"]["proxy"][x] = ag
            P(f"  {x}: Binance-proxy outcome == Polymarket winner {ag:.4f} (n={k.sum()}); when |5m ret|<1bp {np.mean(act[sm] == D[x]['y'][k][sm]):.3f}; "
              f"ties {np.mean(D[x]['r5'] == 0):.4f}; P(up) {D[x]['y'].mean():.4f}")
    for A, B in combinations(assets, 2):
        ya, yb, ra, rb = D[A]["y"], D[B]["y"], D[A]["r5"], D[B]["r5"]
        j = joint(ya, yb)
        rho = float(np.corrcoef(ra, rb)[0, 1])
        dd = np.unique(day)
        dr = np.array([np.corrcoef(ra[day == d], rb[day == d])[0, 1] for d in dd if (day == d).sum() >= 200])
        ds = np.array([np.mean(ya[day == d] == yb[day == d]) for d in dd if (day == d).sum() >= 200])
        hr = (E % 86400) // 3600
        hs = np.array([np.mean(ya[hr == h] == yb[hr == h]) for h in range(24)])
        vt = np.searchsorted(np.quantile(D[A]["sig"], [1 / 3, 2 / 3]), D[A]["sig"])
        vs = [float(np.mean(ya[vt == k] == yb[vt == k])) for k in range(3)]
        mo = np.array([wf.day_of(int(e))[:7] for e in E])
        ms = {str(m): round(float(np.mean(ya[mo == m] == yb[mo == m])), 3) for m in np.unique(mo)}
        q = lambda x: [round(float(np.percentile(x, p)), 3) for p in (0, 10, 50, 90, 100)]  # noqa: E731
        res = dict(joint=j, rho_5m=rho, same_if_bvn=0.5 + np.arcsin(rho) / np.pi, daily_rho=q(dr), daily_same=q(ds),
                   hourly_same_min_max=[float(hs.min()), float(hs.max())], same_by_vol_tercile=vs, same_by_month=ms)
        P(f"\n  {A}-{B}: P(same)={j['same']:.3f} (BVN from 5m-return rho {rho:.3f} -> {res['same_if_bvn']:.3f}) phi={j['phi']:.3f} "
          f"P(A up,B down)={j['a_up_b_down']:.3f} P(A down,B up)={j['a_down_b_up']:.3f}")
        P(f"    stability: daily rho min/p10/med/p90/max {res['daily_rho']}; daily P(same) {res['daily_same']}; "
          f"by UTC hour {hs.min():.3f}..{hs.max():.3f}; by vol tercile {np.round(vs, 3)}; by month {ms}")
        # conditional model: outcome of T given own state, with/without the other asset's state
        for T, O in ((A, B), (B, A)):
            zo, zx = D[T]["z"].ravel(), D[O]["z"].ravel()
            y = np.repeat(D[T]["y"], len(DEC)).astype(float)
            dy = np.repeat(day, len(DEC))
            tdec = np.tile(DEC, len(E))
            X0 = own_X(lgt(ncdf(zo)), (tdec - 150) / 150)
            X1 = np.column_stack([X0, lgt(ncdf(zx))])
            b = logit_fit(X1, y)
            se = cluster_se(X1, y, b, dy)
            p0, p1 = wf_prob(dy, [X0, X1], y, step=7)
            k = np.isfinite(p0)
            dll = boot_mean(logloss(p0[k], y[k]) - logloss(p1[k], y[k]), dy[k])
            dp = 1000 * np.abs(p1[k] - p0[k])
            P(f"    target {T} | other {O}: own-state slope {b[1]:+.3f} (1 = Brownian), other-asset coef {b[-1]:+.3f} (cluster se {se[-1]:.3f}, t={b[-1] / se[-1]:+.1f}); "
              f"OOS logloss gain {dll[0]:+.5f} [{dll[1]:+.5f},{dll[2]:+.5f}]; |dP| mean {dp.mean():.1f} p95 {np.percentile(dp, 95):.1f} milli")
            rows = catchup(zo, zx, y, p0, bin_resid(y, zo, tdec), dy, P, T)
            res[f"target_{T}"] = dict(b=b.tolist(), se=se.tolist(), oos_ll_gain=dll, dp_mean=float(dp.mean()), dp_p95=float(np.percentile(dp, 95)), catchup=rows)
            D[T][f"p0_{O}"] = p0
        # pair edge if each Polymarket leg were priced on its own state only: long laggard UP + leader DOWN
        d = (D[B]["z"] - D[A]["z"]).ravel()
        pa, pb = D[A][f"p0_{B}"], D[B][f"p0_{A}"]
        ya2, yb2 = np.repeat(ya, len(DEC)).astype(float), np.repeat(yb, len(DEC)).astype(float)
        dy = np.repeat(day, len(DEC))
        tt = np.tile(DEC, len(E))
        res["pair_edge"] = []
        for thr in (0.5, 1, 1.5, 2, 3):
            k = np.isfinite(pa) & np.isfinite(pb) & (np.abs(d) >= thr)
            if k.sum() < 200:
                continue
            sgn = np.sign(d[k])  # d>0: B ahead -> A_UP + B_DOWN
            e = 1000 * sgn * ((ya2[k] - pa[k]) - (yb2[k] - pb[k]))
            pay = np.where(sgn > 0, ya2[k] + 1 - yb2[k], 1 - ya2[k] + yb2[k])
            cost = 1000 * np.where(sgn > 0, pa[k] + 1 - pb[k], 1 - pa[k] + pb[k])
            m, l, h = boot_mean(e, dy[k])
            late = tt[k] >= 180
            row = dict(thr=thr, n=int(k.sum()), edge_milli=m, ci90=[l, h], fair_cost=float(cost.mean()),
                       p_pay0=float(np.mean(pay == 0)), p_pay1=float(np.mean(pay == 1)), p_pay2=float(np.mean(pay == 2)),
                       edge_early=float(e[~late].mean()) if (~late).any() else np.nan, edge_late=float(e[late].mean()) if late.any() else np.nan)
            res["pair_edge"].append(row)
            P(f"    pair (long laggard, short leader) |dz|>={thr}: n={row['n']:>7} own-model cost {row['fair_cost']:.0f} payoff 0/1/2 "
              f"{row['p_pay0']:.3f}/{row['p_pay1']:.3f}/{row['p_pay2']:.3f} gross edge {m:+.1f} milli [{l:+.1f},{h:+.1f}] (t<180s {row['edge_early']:+.1f}, t>=180s {row['edge_late']:+.1f})")
        R["under"]["pairs"][f"{A}-{B}"] = res
    P("  read: the pair's gross edge above must beat ~2 half-spreads (~10 milli) + 2 taker fees (up to 35 milli near 50c) to matter.")


# ---------------------------------------------------------------- report: market


def load_pair(out, name):
    fs = sorted((out / "pairs" / name).glob("????-??-??.npz"))
    if not fs:
        return None
    Z = [np.load(f) for f in fs]
    days = np.concatenate([[f.stem] * len(z["epochs"]) for f, z in zip(fs, Z)])
    cat = lambda k: np.concatenate([z[k] for z in Z])  # noqa: E731
    return dict(days=days, udays=[f.stem for f in fs], epochs=cat("epochs"), Wa=cat("Wa"), Wb=cat("Wb"), snap=cat("snap"),
                trig=cat("trig"), mk=cat("mk") if all("mk" in z for z in Z) else None, hist=sum(z["hist"] for z in Z), va=np.mean([z["va"] for z in Z], 0), vb=np.mean([z["vb"] for z in Z], 0))


def calib_rows(Z):
    """-> per orientation-row arrays over valid snapshots: cost_ask, cost_mid, fee, pay, day, t, o"""
    s = Z["snap"].astype(np.float64)
    v = s[..., 0] > 0
    bA, aA, bB, aB = (s[..., k] for k in (1, 2, 3, 4))
    mA, mB = (bA + aA) / 2, (bB + aB) / 2
    Wa, Wb = Z["Wa"][:, None], Z["Wb"][:, None]
    day = np.broadcast_to(Z["days"][:, None], v.shape)
    t = np.broadcast_to(np.array(SNAP_S)[None, :], v.shape)
    out = []
    for o, (ca, cm, f, pay) in enumerate((
        (aA + 1000 - bB, mA + 1000 - mB, fee(aA) + fee(1000 - bB), Wa + 1 - Wb),
        (1000 - bA + aB, 1000 - mA + mB, fee(1000 - bA) + fee(aB), 1 - Wa + Wb),
    )):
        out.append(dict(cost=ca[v], mid=cm[v], fee=f[v], pay=np.broadcast_to(pay, v.shape)[v], day=day[v], t=t[v], o=np.full(v.sum(), o)))
    return {k: np.concatenate([r[k] for r in out]) for k in out[0]}


def score_days(x):
    return float(np.median(x - 0.5 * np.maximum(0, -x))) + 1e-6 * float(np.mean(x))


def walk(Dm, Nm, days, train=14, val=7, topk=20, min_entries=30):
    """Dm/Nm (days, strategies) daily pnl $/trade counts. Rolling selection, then frozen holdout pick."""
    days = np.array(days)
    pre = np.flatnonzero(days < HOLDOUT)
    hold = np.flatnonzero(days >= HOLDOUT)

    def pick(tr, va):
        elig = np.flatnonzero(Nm[tr].sum(0) >= min_entries)
        if not len(elig):
            return None
        top = elig[np.argsort([-score_days(Dm[tr, c]) for c in elig])[:topk]]
        best = max(top, key=lambda c: score_days(Dm[va, c]))
        return int(best) if score_days(Dm[va, best]) > 0 else None

    pnl, picks, cash = [], [], 0
    for s in range(0, len(pre) - train - val):
        c = pick(pre[s : s + train], pre[s + train : s + train + val])
        te = pre[s + train + val]
        cash += c is None
        picks.append(c)
        pnl.append(0.0 if c is None else float(Dm[te, c]))
    res = dict(wf_pre_usd=float(np.sum(pnl)), wf_days=len(pnl), cash_days=int(cash), distinct_picks=len({p for p in picks if p is not None}))
    if len(pre) >= train + val and len(hold):
        c = pick(pre[-train - val : -val], pre[-val:])
        res["frozen_pick"] = c
        res["frozen_hold"] = boot_sum(Dm[hold, c]) if c is not None else (0.0, 0.0, 0.0)
        res["frozen_hold_trades"] = int(Nm[hold, c].sum()) if c is not None else 0
        tot_pre, tot_hold = Dm[pre].sum(0), Dm[hold].sum(0)
        act = Nm[hold].sum(0) > 0
        res["grid_share_positive_hold"] = float((tot_hold[act] > 0).mean()) if act.any() else np.nan
        b = int(np.argmax(tot_pre))
        res["best_hindsight_pre"] = dict(strategy=b, pre_usd=float(tot_pre[b]), hold_usd=float(tot_hold[b]))
    return res


def strat_name(k):
    m, wi = CFG[k // len(VAR)]
    return f"m{m} win{WIN[wi][0]}-{WIN[wi][1]} {VAR[k % len(VAR)]}"


def daily_matrix(Z):
    """-> Dm ($ per day per strategy at SIZE shares), Nm (entries with any fill)"""
    ud = Z["udays"]
    _, inv = np.unique(Z["days"], return_inverse=True)
    T = Z["trig"]
    ns = len(CFG) * len(VAR)
    Dm, Nm = np.zeros((len(ud), ns)), np.zeros((len(ud), ns))
    for ci in range(len(CFG)):
        filled = T[:, ci, 0] >= 1
        for vi in range(len(VAR)):
            k = ci * len(VAR) + vi
            Dm[:, k] = np.bincount(inv, T[:, ci, 5 + vi] * SIZE / 1000, len(ud))
            Nm[:, k] = np.bincount(inv, filled, len(ud))
    return Dm, Nm


def strat_line(Z, ci, P, tag="", days_mask=None):
    T = Z["trig"][:, ci]
    k = np.ones(len(T), bool) if days_mask is None else days_mask
    code = T[k, 0]
    both, orphan = code == 1, code >= 2
    trig_n = int((code >= 0).sum())
    hold = np.array([d >= HOLDOUT for d in Z["days"][k]])
    pnl = T[k, 5].astype(float) * SIZE / 1000
    row = dict(markets=int(k.sum()), triggers=trig_n, both=int(both.sum()), orphan=int(orphan.sum()), none=int((code == 0).sum()))
    if both.any():
        pay = T[k, 4][both]
        row.update(cost_mean=float(np.nanmean(T[k, 3][both])), pay0=float(np.mean(pay == 0)), pay1=float(np.mean(pay == 1)), pay2=float(np.mean(pay == 2)),
                   pair_mean_milli=float(T[k, 5][both].mean()), t_med=float(np.nanmedian(T[k, 2][both])))
    if orphan.any():
        row["orphan_mean_milli"] = float(T[k, 5][orphan].mean())
    for nm, sel in (("pre", ~hold), ("hold", hold)):
        ud, inv = np.unique(Z["days"][k][sel], return_inverse=True)
        row[nm] = boot_sum(np.bincount(inv, pnl[sel])) if len(ud) else (0.0, np.nan, np.nan)
    _, inv = np.unique(Z["days"][k], return_inverse=True)
    dp = np.bincount(inv, pnl)
    row["worst_day_usd"], row["max_dd_usd"] = float(dp.min()), float((np.maximum.accumulate(np.cumsum(dp)) - np.cumsum(dp)).max())
    P(f"    {tag}{strat_name(ci * len(VAR))[:-5]:<16} mkts {row['markets']:>6} trig {trig_n:>5} both {row['both']:>5} orphan {row['orphan']:>4} | "
      f"cost {row.get('cost_mean', np.nan):6.0f} pay 0/1/2 {row.get('pay0', np.nan):.3f}/{row.get('pay1', np.nan):.3f}/{row.get('pay2', np.nan):.3f} "
      f"pair {row.get('pair_mean_milli', np.nan):+7.1f}m orphan {row.get('orphan_mean_milli', np.nan):+7.1f}m | "
      f"pre ${row['pre'][0]:+8.2f} [{row['pre'][1]:+.2f},{row['pre'][2]:+.2f}] hold ${row['hold'][0]:+8.2f} [{row['hold'][1]:+.2f},{row['hold'][2]:+.2f}] worst day ${row['worst_day_usd']:+.2f} maxDD ${row['max_dd_usd']:.2f}")
    return row


def cross_market(Z, A, B, P):
    """Does the other market's price predict this market's outcome beyond its own price? + single-leg model trades."""
    s = Z["snap"].astype(np.float64)
    v = s[..., 0] > 0
    n, ns = v.shape
    day = np.broadcast_to(Z["days"][:, None], v.shape)
    res = {}
    for T, own, oth, W, szU, szD in ((A, (1, 2), (3, 4), Z["Wa"], 5, 6), (B, (3, 4), (1, 2), Z["Wb"], 7, 8)):
        b_, a_ = s[..., own[0]], s[..., own[1]]
        lo = np.log(np.clip((b_ + a_) / 2, 5, 995) / (1000 - np.clip((b_ + a_) / 2, 5, 995)))
        mo = np.clip((s[..., oth[0]] + s[..., oth[1]]) / 2, 5, 995)
        lx = np.log(mo / (1000 - mo))
        y = np.broadcast_to(W[:, None], v.shape).astype(float)
        f = v.ravel()
        X0 = own_X(lo.ravel()[f])
        X1 = np.column_stack([X0, lx.ravel()[f]])
        yy, dd = y.ravel()[f], day.ravel()[f]
        if len(np.unique(dd)) < 16:
            P(f"    target {T}: fewer than 16 days, skipped")
            continue
        b = logit_fit(X1, yy)
        se = cluster_se(X1, yy, b, dd)
        p0, p1 = wf_prob(dd, [X0, X1], yy, step=3)
        k = np.isfinite(p0)
        dll = boot_mean(logloss(p0[k], yy[k]) - logloss(p1[k], yy[k]), dd[k])
        mid = sig(lo.ravel()[f])
        llm = boot_mean(logloss(mid[k], yy[k]) - logloss(p1[k], yy[k]), dd[k])
        P(f"    target {T}: own-price slope {b[1]:+.3f}, other-market coef {b[-1]:+.3f} (cluster se {se[-1]:.3f}, t={b[-1] / se[-1]:+.1f}); "
          f"OOS logloss gain vs own-price model {dll[0]:+.5f} [{dll[1]:+.5f},{dll[2]:+.5f}], vs raw mid {llm[0]:+.5f} [{llm[1]:+.5f},{llm[2]:+.5f}]")
        r = dict(b=b.tolist(), se=se.tolist(), oos_gain_vs_own=dll, oos_gain_vs_mid=llm, trades={})
        for nm, p in (("cross", p1), ("own", p0)):
            pg = np.full(n * ns, np.nan)
            pg[f] = p
            pg = pg.reshape(n, ns)
            au, ad = a_, 1000 - b_
            eu = np.where(s[..., szU] >= SIZE, 1000 * pg - au - fee(au), -np.inf)
            ed = np.where(s[..., szD] >= SIZE, 1000 * (1 - pg) - ad - fee(ad), -np.inf)
            e = np.nan_to_num(np.maximum(eu, ed), nan=-np.inf)
            for mg in (20, 40):
                ok = e >= mg
                has = ok.any(1)
                i = ok.argmax(1)
                up = take(eu, i) >= take(ed, i)
                px = np.where(up, take(au, i), take(ad, i))
                pnl = np.where(has, 1000 * np.where(up, W, 1 - W) - px - fee(px), 0.0) * SIZE / 1000
                ud, inv = np.unique(Z["days"], return_inverse=True)
                tot = boot_sum(np.bincount(inv, pnl))
                r["trades"][f"{nm}_m{mg}"] = dict(n=int(has.sum()), usd=tot, mean_milli=float(pnl[has].mean() * 1000 / SIZE) if has.any() else np.nan)
                P(f"      single-leg {nm:>5}-model trades, margin {mg}: n={int(has.sum()):>5} total ${tot[0]:+8.2f} [{tot[1]:+.2f},{tot[2]:+.2f}] mean {r['trades'][f'{nm}_m{mg}']['mean_milli']:+.1f} milli (no latency: optimistic)")
        res[T] = r
    return res


def mk_name(ci):
    m, wi, ttl, imp, fm = MK_CFG[ci]
    return f"mid<={1000 - m} t{MK_WIN[wi][0]}-{MK_WIN[wi][1]} ttl{ttl} imp{imp} {'touch' if fm else 'thru'}"


def rep_maker(Z, P):
    """S7 two-legged maker pair: both bids rest at best bid; quote-proxy fills on the 250 ms grid."""
    P("  S7 two-legged MAKER pair (both bids at best bid, fee 0, quote-proxy fills; pnl $ at 5 sh; orphan held or completed as taker):")
    hold = np.array([d >= HOLDOUT for d in Z["days"]])
    inc = ~np.isin(Z["days"], INCIDENT)
    rows = []
    for ci in range(len(MK_CFG)):
        X = Z["mk"][:, ci]
        code = X[:, 0]
        r = dict(cfg=mk_name(ci), posted=int((code >= 0).sum()))
        r["both"] = float((code == 1).sum() / max(r["posted"], 1))
        r["one"] = float((code >= 2).sum() / max(r["posted"], 1))
        r["gap_mid"] = float(np.nanmean(X[code >= 0, 1])) if r["posted"] else np.nan
        r["pair_mean_milli"] = float(X[code == 1, 2].mean()) if (code == 1).any() else np.nan
        r["orphan_hold_milli"] = float(X[code >= 2, 2].mean()) if (code >= 2).any() else np.nan
        r["orphan_comp_milli"] = float(X[code >= 2, 3].mean()) if (code >= 2).any() else np.nan
        for j, nm in ((2, "hold"), (3, "comp")):
            pnl = X[:, j].astype(float) * SIZE / 1000
            for tag, sel in (("pre", ~hold), ("ho", hold), ("pre_ex", ~hold & inc)):
                ud, inv = np.unique(Z["days"][sel], return_inverse=True)
                r[f"{nm}_{tag}"] = boot_sum(np.bincount(inv, pnl[sel])) if len(ud) else (0.0, np.nan, np.nan)
        rows.append(r)

    def line(r, tag=""):
        P(f"    {tag}{r['cfg']:<36} posted {r['posted']:>5} both {r['both']:.2f} one {r['one']:.2f} gap@mid {r['gap_mid']:5.1f} | "
          f"pair {r['pair_mean_milli']:+6.1f}m orphan hold {r['orphan_hold_milli']:+7.1f}m comp {r['orphan_comp_milli']:+7.1f}m | "
          f"complete: pre ${r['comp_pre'][0]:+8.2f} [{r['comp_pre'][1]:+.2f},{r['comp_pre'][2]:+.2f}] ex-Jun1-3 ${r['comp_pre_ex'][0]:+8.2f} "
          f"hold-out ${r['comp_ho'][0]:+7.2f} | settle: pre ${r['hold_pre'][0]:+8.2f} hold-out ${r['hold_ho'][0]:+7.2f}")

    line(rows[MK_PRE], "PRE-REGISTERED ")
    for r in sorted(rows, key=lambda r: -r["comp_pre"][0])[:8]:
        line(r)
    act = [r for r in rows if r["posted"]]
    best = max(act, key=lambda r: r["comp_pre"][0]) if act else None
    if best is not None:
        P(f"    best-in-hindsight pre: {best['cfg']} ${best['comp_pre'][0]:+.2f} -> hold-out ${best['comp_ho'][0]:+.2f}; "
          f"share of {len(act)} configs positive pre {np.mean([r['comp_pre'][0] > 0 for r in act]):.2f}, "
          f"hold-out {np.mean([r['comp_ho'][0] > 0 for r in act]):.2f} (hold-out = {int(hold.sum())} markets)")
    return dict(pre_registered=rows[MK_PRE], rows=rows)


def rep_pairs(a, R, P):
    P("\n== S3-S6 Polymarket pair tests (250 ms grid, fresh + mirrored books, latency 250 ms, 5 shares, taker fee 0.07*p*(1-p)) ==")
    pd_ = a.out / "pairs"
    names = sorted(p.name for p in pd_.iterdir() if p.is_dir()) if pd_.is_dir() else []
    if not names:
        P("  no pair scans (need quote grids for >= 2 assets: run grid + pairs)")
        return
    R["pairs"] = {}
    for name in names:
        Z = load_pair(a.out, name)
        if Z is None:
            continue
        A, B = name.split("-")
        res = dict(markets=len(Z["epochs"]), days=len(Z["udays"]), first=Z["udays"][0], last=Z["udays"][-1])
        P(f"\n  -- {name}: {res['markets']} markets / {res['days']} days {res['first']}..{res['last']}; grid valid share {A} {Z['va'][0]:.3f} (pre-mirror {Z['va'][1]:.3f}) "
          f"{B} {Z['vb'][0]:.3f} (pre-mirror {Z['vb'][1]:.3f})")
        H = Z["hist"]
        res["time_share_cost_le"] = {f"{WIN[w][0]}-{WIN[w][1]}" if w < len(WIN) else "all": {m: float(H[w, 1 + i] / max(H[w, 0], 1)) for i, m in enumerate(M)} for w in range(len(WIN) + 1)}
        P(f"  S3 share of time the cheaper orientation costs <= $1-m after fees (m milli): { {m: round(x, 4) for m, x in res['time_share_cost_le']['all'].items()} }")
        P(f"     last minute (240-290 s): { {m: round(x, 4) for m, x in res['time_share_cost_le']['240-290'].items()} }")
        C = calib_rows(Z)
        P("  S4 calibration at 10 s snapshots, both orientations (cost = ask_A_leg + ask_B_leg, milli):")
        P("       cost bucket        n   cost   pay0  pay1  pay2 | edge@mid [90% CI]        edge@ask  net of fees [90% CI]")
        res["calib"] = []
        for lo, hi in zip(CB[:-1], CB[1:]):
            k = (C["cost"] >= lo) & (C["cost"] < hi)
            if k.sum() < 50:
                continue
            em = boot_mean(1000 * C["pay"][k] - C["mid"][k], C["day"][k])
            en = boot_mean(1000 * C["pay"][k] - C["cost"][k] - C["fee"][k], C["day"][k])
            ea = float((1000 * C["pay"][k] - C["cost"][k]).mean())
            row = dict(lo=lo, hi=hi, n=int(k.sum()), cost=float(C["cost"][k].mean()), pay=[float(np.mean(C["pay"][k] == q)) for q in (0, 1, 2)], edge_mid=em, edge_ask=ea, edge_net=en)
            res["calib"].append(row)
            P(f"     [{lo:>4},{hi:>4}) {row['n']:>8} {row['cost']:6.0f}  {row['pay'][0]:.3f} {row['pay'][1]:.3f} {row['pay'][2]:.3f} | {em[0]:+7.1f} [{em[1]:+.1f},{em[2]:+.1f}]  {ea:+7.1f}  {en[0]:+7.1f} [{en[1]:+.1f},{en[2]:+.1f}]")
        res["calib_cheap_by_time"] = []
        for t0, t1 in ((10, 90), (100, 190), (200, 290)):
            k = (C["cost"] < 950) & (C["t"] >= t0) & (C["t"] <= t1)
            if k.sum() >= 50:
                em = boot_mean(1000 * C["pay"][k] - C["mid"][k], C["day"][k])
                res["calib_cheap_by_time"].append(dict(t0=t0, t1=t1, n=int(k.sum()), edge_mid=em))
                P(f"     cost<950, t {t0}-{t1}s: n={k.sum()} edge@mid {em[0]:+.1f} [{em[1]:+.1f},{em[2]:+.1f}] pay0 {np.mean(C['pay'][k] == 0):.3f}")
        P("  S5 taker entries (first trigger per market; IOC both legs at trigger asks 250 ms later; an orphan leg is held to settlement):")
        res["pre_registered"] = strat_line(Z, PRE, P, "PRE-REGISTERED ")
        inc = ~np.isin(Z["days"], INCIDENT)
        if (~inc).any():
            res["pre_registered_ex_incident"] = strat_line(Z, PRE, P, "  ex Jun 1-3   ", inc)
        res["by_margin"] = {m: strat_line(Z, CFG.index((m, 0)), P) for m in M if m != 50}
        res["by_window_m50"] = {w: strat_line(Z, CFG.index((50, w)), P) for w in range(1, len(WIN))}
        Dm, Nm = daily_matrix(Z)
        Z["Dm"], Z["Nm"] = Dm, Nm
        res["walk"] = walk(Dm, Nm, Z["udays"])
        w = res["walk"]
        P(f"    walk-forward over {Dm.shape[1]} strategies (margin x window x exit): pre ${w['wf_pre_usd']:+.2f} over {w['wf_days']} days, cash {w['cash_days']} days, {w['distinct_picks']} distinct picks")
        if "frozen_hold" in w:
            fp = "cash" if w["frozen_pick"] is None else strat_name(w["frozen_pick"])
            bh = w["best_hindsight_pre"]
            P(f"    frozen holdout pick: {fp}: ${w['frozen_hold'][0]:+.2f} [{w['frozen_hold'][1]:+.2f},{w['frozen_hold'][2]:+.2f}] on {w['frozen_hold_trades']} trades; "
              f"share of grid positive in holdout {w['grid_share_positive_hold']:.2f}; best-in-hindsight pre {strat_name(bh['strategy'])} ${bh['pre_usd']:+.2f} -> holdout ${bh['hold_usd']:+.2f}")
        P("  S6 cross-market efficiency (walk-forward logistic on 10 s snapshots):")
        res["cross"] = cross_market(Z, A, B, P)
        if Z["mk"] is not None:
            res["maker"] = rep_maker(Z, P)
        R["pairs"][name] = res


def cmd_report(a):
    R = {}
    lines = []

    def P(s=""):
        print(s, flush=True)
        lines.append(s)

    for fn in (rep_outcomes, rep_under, rep_pairs):
        try:
            fn(a, R, P)
        except Exception:  # noqa: BLE001 - one broken section must not hide the others
            P(f"!! {fn.__name__} failed:\n{traceback.format_exc()}")
    (a.out / "report.json").write_text(json.dumps(R, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    (a.out / "report.txt").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    AS = lambda s: [x.lower() for x in s.split(",") if x]  # noqa: E731

    def add(name, **kw):
        p = sp.add_parser(name)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--btc-cache", type=Path, default=None)
        p.add_argument("--assets", type=AS, default=kw.get("assets", ["btc", "eth", "sol", "xrp"]))
        p.add_argument("--workers", type=int, default=4)
        return p

    p = add("inv")
    p.add_argument("--data", type=Path, nargs="+", required=True)
    p = add("klines")
    p.add_argument("--start", default="2026-04")
    p.add_argument("--end", default="2026-07")
    add("settle", assets=["eth", "sol", "xrp"])
    p = add("grid")
    p.add_argument("--npz", nargs="*", default=[], help="asset=wf2_npz_cache_dir")
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--force", action="store_true")
    add("pairs")
    add("report")
    a = ap.parse_args()
    {"inv": cmd_inv, "klines": cmd_klines, "settle": cmd_settle, "grid": cmd_grid, "pairs": cmd_pairs, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
