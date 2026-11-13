"""Martingale / scaled maker ladders on BOTH sides of BTC 5m up/down books. One pass over the stwf2 npz cache.

F1 static two-sided ladders: rung j at bid(s) - j*step on UP and DOWN, cancel at C.
   Scan stores per-rung fill capacity under 4 fill models; report applies 8 scaling models x K x pmax.
F2 dynamic pair-cost martingale (gabagool-style): requote both bids every R s, price capped so
   avg_other + px <= target, size grows per fill (mart) or to rebalance inventory (bal).
F3 zone-recovery hedge martingale (taker): flip side each zone break, size so the newest side wins >= target.
F4 cross-market sequence sizing (report): martingale / anti / d'Alembert on per-market PnL of F1/F2 picks.

Fill models (nested, each a superset of the previous):
  through  ask < px                       (quote proxy, conservative)       cap = max ask size at crossing
  touch    ask <= px                                                         cap = max ask size at crossing
  level    best bid < px at any time       (level consumed OR pulled: optimistic)   cap = displayed size at px
  queue    + every best-bid size decrement while bid == px is our fill (queue-front UPPER BOUND)
"""

import argparse
import csv
import json
import math
from itertools import product
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from same_token_wf2 import AGE_US, LAT_US, MKT_US, SIZE, day_of, done_days, markets, settlements

FEE, REB = 0.07, 0.20
HOLDOUT = "2026-07-19"
BAD = ("2026-06-01", "2026-06-02", "2026-06-03")
BASE = 5.0
NR = 12
MODELS = ("through", "touch", "level", "queue")
S1 = (5, 30, 60, 120, 200, 240)
STEP = (10, 20, 30, 50)
CANCEL = (120, 240, 300)
C1 = list(product(range(len(S1)), range(len(STEP)), range(len(CANCEL))))
SCALES = ("flat", "geo0.5", "linear", "geo1.25", "geo1.5", "geo2", "geo3", "fib", "invp", "recover")
KS = (1, 2, 3, 5, 8, 12)
PMAX = (990, 500, 300)
MAXQ = 20000.0
# F2
F2 = list(
    product(
        (950, 970, 990, 1000),  # target pair cost
        (500, 450),  # max price for the first leg
        (("mart", 1.5), ("mart", 2.0), ("bal", 1.0), ("bal", 2.0)),
        (240, 285),  # stop adding (s)
        (1, 3),  # model index: touch, queue
    )
)
F2_R, F2_S, F2_MAXQ = 5, 5, 500.0
# F3
F3 = list(product((50, 100, 150, 200), (0.25, 1.0), (3, 5, 8), (5, 60, 150), (0, 1), (0, 1)))  # z,tfrac,n,s,side,cap
BIG = 1 << 62


def fee(p):
    return FEE * p * (1000 - p) / 1000.0  # milli per share


def scale_sizes(name, px):
    """px: (..., NR) rung prices -> sizes (..., NR) in shares."""
    j = np.arange(NR, dtype=float)
    if name == "flat":
        s = np.ones(NR)
    elif name == "linear":
        s = j + 1
    elif name.startswith("geo"):
        s = float(name[3:]) ** j
    elif name == "invp":  # equal dollars per rung
        return np.where(px > 0, BASE * 500.0 / np.maximum(px, 1), 0)
    elif name == "fib":
        f = [1.0, 1.0]
        while len(f) < NR:
            f.append(f[-1] + f[-2])
        s = np.array(f[:NR])
    elif name != "recover":
        raise ValueError(name)
    else:  # recover: if this side wins after rung j, side PnL >= BASE*(1000-p0)
        p = px.astype(float)
        out = np.zeros(p.shape)
        Q = np.zeros(p.shape[:-1])
        C = np.zeros(p.shape[:-1])
        tgt = BASE * (1000 - p[..., 0])
        for k in range(NR):
            d = np.maximum(1000 - p[..., k], 1)
            x = np.clip(np.ceil((C + tgt - Q * 1000) / d), BASE, MAXQ)
            x = np.where(p[..., k] > 0, x, 0)
            out[..., k] = x
            Q += x
            C += x * p[..., k]
        return out
    return np.broadcast_to(np.minimum(BASE * s, MAXQ), px.shape)


def sides(m):
    t, bid, ask = m["t"], m["bid"].astype(np.int64), m["ask"].astype(np.int64)
    gs = (bid >= 0) & ((t[:, None] - m["seen"]) <= AGE_US)
    out = []
    for k in (0, 1):
        e = np.flatnonzero(gs[:, k] & m["upd"][:, k])
        b, z = bid[e, k], m["bsz"][e, k].astype(float)
        dz = np.zeros(len(e))
        if len(e) > 1:
            same = b[1:] == b[:-1]
            dz[1:] = np.where(same, np.maximum(z[:-1] - z[1:], 0), 0)
        out.append(dict(t=t[e], a=ask[e, k], b=b, bsz=z, asz=m["asz"][e, k].astype(float), dz=dz))
    return gs, out


def quote_at(t, good, us):
    """first fully valid quote at/after us (within AGE)."""
    gi = np.flatnonzero(good)
    k = np.searchsorted(t[gi], us)
    if k >= len(gi) or t[gi[k]] - us > AGE_US:
        return -1
    return int(gi[k])


# ---------------------------------------------------------------- F1


def f1(m, gs, S):
    t, bid = m["t"], m["bid"].astype(np.int64)
    good = gs.all(1)
    PX = np.zeros((len(C1), 2, NR), np.int16)
    AV = np.zeros((len(C1), 4, 2, NR), np.float16)
    for si, s in enumerate(S1):
        i0 = quote_at(t, good, s * 1_000_000)
        if i0 < 0:
            continue
        tp = t[i0] + LAT_US
        for k in (0, 1):
            E = S[k]
            lo = int(np.searchsorted(E["t"], tp))
            for pi, st in enumerate(STEP):
                px = bid[i0, k] - st * np.arange(NR)
                px = np.where(px >= 10, px, 0)
                for ci, c in enumerate(CANCEL):
                    if c <= s:
                        continue
                    hi = int(np.searchsorted(E["t"], c * 1_000_000))
                    idx = C1.index((si, pi, ci))
                    PX[idx, k] = px
                    if hi <= lo:
                        continue
                    a, b, asz, bsz, dz = (E[x][lo:hi, None] for x in ("a", "b", "asz", "bsz", "dz"))
                    P = px[None, :]
                    at = b == P
                    av0 = ((a < P) * asz).max(0)
                    av1 = ((a <= P) * asz).max(0)
                    van = (b < P).any(0)
                    lv = np.maximum((at * bsz).max(0), av1)  # level size we could have been part of
                    lv = np.where(lv > 0, lv, BASE)
                    q = (at * dz).sum(0)
                    ok = px > 0
                    AV[idx, 0, k] = np.where(ok, np.minimum(av0, 60000), 0)
                    AV[idx, 1, k] = np.where(ok, np.minimum(av1, 60000), 0)
                    AV[idx, 2, k] = np.where(ok & van, np.minimum(lv, 60000), 0)
                    AV[idx, 3, k] = np.where(ok, np.minimum(q + van * lv, 60000), 0)
    return PX, AV


# ---------------------------------------------------------------- F2


def window_fill(E, lo, hi, px, size, model):
    if hi <= lo or size <= 0:
        return 0.0
    if model == 1:  # touch
        w = E["a"][lo:hi] <= px
        if not w.any():
            return 0.0
        return float(min(size, E["asz"][lo:hi][w].max()))
    b, at = E["b"][lo:hi], E["b"][lo:hi] == px
    q = E["dz"][lo:hi][at].sum()
    if (b < px).any():
        lv = max(E["bsz"][lo:hi][at].max() if at.any() else 0.0, BASE)
        q += lv
    return float(min(size, q))


def f2(m, gs, S, W):
    t, bid = m["t"], m["bid"].astype(np.int64)
    good = gs.all(1)
    ticks = []
    for tau in range(F2_S, 300, F2_R):
        qi = quote_at(t, good, tau * 1_000_000)
        if qi < 0 or t[qi] >= (tau + F2_R) * 1_000_000:
            continue
        tp = t[qi] + LAT_US
        w = [(int(np.searchsorted(S[k]["t"], tp)), int(np.searchsorted(S[k]["t"], tp + F2_R * 1_000_000))) for k in (0, 1)]
        ticks.append((tau, bid[qi], w))
    out = np.full((len(F2), 6), np.nan, np.float32)  # pnl, reb, notional, fills, pairq, maxside
    for ci, (tgt, emax, (rule, g), tstop, model) in enumerate(F2):
        q, c, nf, reb, fills = [0.0, 0.0], [0.0, 0.0], [0, 0], 0.0, 0
        for tau, bq, w in ticks:
            if tau > tstop:
                break
            orders = []
            for X in (0, 1):
                o = 1 - X
                px = int(bq[X])
                px = min(px, tgt - math.ceil(c[o] / q[o])) if q[o] > 0 else min(px, emax)
                px = (px // 10) * 10
                if px < 10:
                    continue
                if rule == "mart":
                    sz = BASE * g ** nf[X]
                elif q[X] < q[o]:
                    sz = max(BASE, g * (q[o] - q[X]))
                else:
                    sz = BASE if (q[X] == 0 or px <= c[X] / q[X] - 10) else 0.0
                sz = min(sz, F2_MAXQ - q[X])
                if sz >= BASE:
                    orders.append((X, px, sz))
            for X, px, sz in orders:
                f = window_fill(S[X], *w[X], px, sz, model)
                if f > 0:
                    q[X] += f
                    c[X] += f * px
                    nf[X] += 1
                    fills += 1
                    reb += f * REB * fee(px)
        if W is not None:
            out[ci] = (q[W] * 1000 - c[0] - c[1], reb, c[0] + c[1], fills, min(q), max(q))
    return out


# ---------------------------------------------------------------- F3


def f3(m, gs, W):
    t, bid, ask, asz = m["t"], m["bid"].astype(np.int64), m["ask"].astype(np.int64), m["asz"]
    good = gs.all(1)
    gi = np.flatnonzero(good)
    if len(gi) < 2:
        return np.full((len(F3), 5), np.nan, np.float32)
    tg = t[gi]
    mid = (bid[gi, 0] + ask[gi, 0]) / 2.0

    def fill_at(us):  # last good quote at/before us+LAT
        k = np.searchsorted(tg, us + LAT_US, "right") - 1
        return k if k >= 0 and us + LAT_US - tg[k] <= AGE_US else -1

    out = np.full((len(F3), 5), np.nan, np.float32)  # pnl, fees, notional, steps, short(=depth-limited)
    for ci, (z, tf, nmax, s, A, cap) in enumerate(F3):
        k0 = np.searchsorted(tg, s * 1_000_000)
        if k0 >= len(gi) or W is None:
            continue
        f = fill_at(tg[k0])
        if f < 0:
            continue
        L = mid[f]
        lo_, hi_ = (L - z, L) if A == 0 else (L, L + z)
        Q, Cst, fees, steps, short = [0.0, 0.0], 0.0, 0.0, 0, 0
        X, kk, T = A, f, None
        while steps < nmax:
            p = int(ask[gi[kk], X])
            if T is None:
                T = tf * BASE * (1000 - p)
            fp = fee(p)
            d = 1000 - p - fp
            if d <= 0:
                break
            x = max(BASE, math.ceil((T + Cst - Q[X] * 1000) / d))
            if cap:
                av = float(asz[gi[kk], X])
                if av < x:
                    short += 1
                    x = av
            Q[X] += x
            Cst += x * (p + fp)
            fees += x * fp
            steps += 1
            # next trigger
            seg = mid[kk + 1 :]
            hit = np.flatnonzero(seg <= lo_) if X == 0 else np.flatnonzero(seg >= hi_)
            if not len(hit):
                break
            j = kk + 1 + int(hit[0])
            if tg[j] > 295_000_000:
                break
            kk = fill_at(tg[j])
            if kk < 0:
                break
            X = 1 - X
        out[ci] = (Q[W] * 1000 - Cst, fees, Cst, steps, short)
    return out


# ---------------------------------------------------------------- scan


def _day(args):
    ddir, out, sett, skip = args
    E, WS, PXs, AVs, F2s, F3s = [], [], [], [], [], []
    for e, m in markets(ddir):
        if len(m["t"]) < 2:
            continue
        W = sett.get(e)
        gs, S = sides(m)
        PX, AV = f1(m, gs, S)
        E.append(e)
        WS.append(-1 if W is None else W)
        PXs.append(PX)
        AVs.append(AV)
        F2s.append(f2(m, gs, S, W) if "f2" not in skip else np.full((len(F2), 6), np.nan, np.float32))
        F3s.append(f3(m, gs, W) if "f3" not in skip else np.full((len(F3), 5), np.nan, np.float32))
    if E:
        tmp = out / f".{ddir.name}.tmp"
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, epoch=np.array(E), W=np.array(WS, np.int8), PX=np.stack(PXs), AV=np.stack(AVs), F2=np.stack(F2s), F3=np.stack(F3s))
        tmp.replace(out / f"{ddir.name}.npz")
    return ddir.name, len(E)


def cmd_scan(a):
    a.out.mkdir(parents=True, exist_ok=True)
    sett = settlements(a.cache)
    days = done_days(a.cache)[: a.days or None]
    skip = set(a.skip.split(",")) if a.skip else set()
    jobs = [(d, a.out, sett, skip) for d in days if a.force or not (a.out / f"{d.name}.npz").exists()]
    with Pool(a.workers) as p:
        for d, k in p.imap_unordered(_day, jobs):
            print(d, k, flush=True)


# ---------------------------------------------------------------- report


class Book:
    """config x day matrices: pnl, reb, notional (milli), + per-config risk stats."""

    def __init__(self, days):
        self.days = days
        self.names, self.P, self.R, self.N, self.risk = [], [], [], [], []

    def add(self, name, dix, pnl, reb, notl, maxcost=None):
        nd = len(self.days)
        self.names.append(name)
        self.P.append(np.bincount(dix, pnl, nd))
        self.R.append(np.bincount(dix, reb, nd))
        self.N.append(np.bincount(dix, notl, nd))
        mc = maxcost if maxcost is not None else notl
        self.risk.append((float(pnl.min()) if len(pnl) else 0.0, float(np.percentile(mc, 99)) if len(mc) else 0.0, float(mc.max()) if len(mc) else 0.0, int((notl > 0).sum())))

    def mats(self):
        return np.array(self.P), np.array(self.R), np.array(self.N)


_BC = {}


def boot_counts(nd, B=1000, seed=0):
    if (nd, seed) not in _BC:
        r = np.random.default_rng(seed + nd)
        idx = r.integers(0, nd, (B, nd))
        _BC[(nd, seed)] = np.stack([np.bincount(i, minlength=nd) for i in idx], 1).astype(float)
    return _BC[(nd, seed)]


def stats(P, N, sel, _=None, chunk=2000):
    """per-config over sel days: usd, cents per $ notional, 90% day-bootstrap CI, usd/day, volume k$."""
    p, n = P[:, sel], N[:, sel]
    C = boot_counts(int(sel.sum()))
    lo, hi = np.empty(len(p)), np.empty(len(p))
    for i in range(0, len(p), chunk):
        r = 100 * (p[i : i + chunk] @ C) / np.maximum(n[i : i + chunk] @ C, 1)
        lo[i : i + chunk], hi[i : i + chunk] = np.percentile(r, 5, 1), np.percentile(r, 95, 1)
    usd = p.sum(1) / 1000
    cpd = 100 * p.sum(1) / np.maximum(n.sum(1), 1)
    return usd, cpd, lo, hi, usd / max(sel.sum(), 1), n.sum(1) / 1000


def tstat(P, N):
    """day-clustered t-stat of the ratio sum(P)/sum(N), per config (rows)."""
    r = P.sum(1) / np.maximum(N.sum(1), 1)
    e = P - r[:, None] * N
    se = np.sqrt((e**2).sum(1)) / np.maximum(N.sum(1), 1)
    return np.where(se > 0, r / np.maximum(se, 1e-12), 0.0)


def wf(P, N, ok, train=14, test=7, minvol=None):
    """rolling: pick the best train t-stat among ok configs, apply next test days. returns daily oos pnl, notional, picks."""
    nd = P.shape[1]
    op, on, picks = np.zeros(nd), np.zeros(nd), []
    i = train
    while i < nd:
        tr = slice(i - train, i)
        sc = tstat(P[:, tr], N[:, tr])
        if minvol is not None:
            sc = np.where(N[:, tr].sum(1) >= minvol, sc, -np.inf)
        sc = np.where(ok, sc, -np.inf)
        b = int(np.argmax(sc))
        te = slice(i, min(i + test, nd))
        op[te], on[te] = P[b, te], N[b, te]
        picks.append(b)
        i += test
    return op, on, picks


def cmd_report(a):
    fs = sorted(a.out.glob("????-??-??.npz"))
    Z = [np.load(f) for f in fs]
    days = [f.stem for f in fs]
    nd = len(days)
    ep = np.concatenate([z["epoch"] for z in Z])
    W = np.concatenate([z["W"] for z in Z]).astype(int)
    dix = np.concatenate([np.full(len(z["epoch"]), i) for i, z in enumerate(Z)])
    PX = np.concatenate([z["PX"] for z in Z]).astype(np.float32)
    AV = np.concatenate([z["AV"] for z in Z]).astype(np.float32)
    F2a = np.concatenate([z["F2"] for z in Z]).astype(float)
    F3a = np.concatenate([z["F3"] for z in Z]).astype(float)
    ok = W >= 0
    ep, W, dix, PX, AV, F2a, F3a = ep[ok], W[ok], dix[ok], PX[ok], AV[ok], F2a[ok], F3a[ok]
    o = np.argsort(ep)
    ep, W, dix, PX, AV, F2a, F3a = ep[o], W[o], dix[o], PX[o], AV[o], F2a[o], F3a[o]
    M = len(ep)
    prevW = np.full(M, -1)
    cons = np.r_[False, np.diff(ep) == 300]
    prevW[cons] = W[np.flatnonzero(cons) - 1]
    hold = np.array([d >= HOLDOUT for d in days])
    bad = np.array([d in BAD for d in days])
    dev = ~hold & ~bad
    Cn = None
    win = np.stack([W == 0, W == 1], 1).astype(float)[:, :, None]  # (M,2,1)
    rep = dict(days=nd, markets=M, first=days[0], last=days[-1], dev_days=int(dev.sum()), hold_days=int(hold.sum()))
    rep["outcome_same_as_prev"] = float((W[cons] == prevW[cons]).mean()) if cons.any() else None

    # ---- atlas: per-share markout of a flat 1-share rung, by model x start x depth x price bucket
    atlas = []
    pb = (0, 200, 400, 600, 800, 1001)
    for mi, mn in enumerate(MODELS):
        for si, s in enumerate(S1):
            for pi, st in enumerate(STEP):
                idx = C1.index((si, pi, len(CANCEL) - 1))
                px, fil = PX[:, idx], AV[:, idx, mi] > 0
                pnl = (1000 * win - px)
                for j0, j1 in ((0, 1), (1, 3), (3, 6), (6, 12)):
                    for b0, b1 in zip(pb[:-1], pb[1:]):
                        sel = fil[:, :, j0:j1] & (px[:, :, j0:j1] >= b0) & (px[:, :, j0:j1] < b1)
                        for tag, dm in (("dev", dev), ("hold", hold)):
                            ss = sel & dm[dix][:, None, None]
                            n = int(ss.sum())
                            if n < 50:
                                continue
                            v = pnl[:, :, j0:j1][ss]
                            rb = REB * fee(px[:, :, j0:j1][ss])
                            atlas.append(dict(model=mn, s=s, step=st, rungs=f"{j0}-{j1 - 1}", px=f"{b0}-{b1 - 1}", split=tag, n=n, milli=round(float(v.mean()), 2), milli_reb=round(float((v + rb).mean()), 2), se=round(float(v.std() / math.sqrt(n)), 2)))
    rep["atlas"] = atlas
    # previous-winner split, flat, s=5 step 10 all rungs, dev
    pw = []
    idx = C1.index((0, 0, len(CANCEL) - 1))
    for mi, mn in enumerate(MODELS):
        fil = AV[:, idx, mi] > 0
        pnl = 1000 * win - PX[:, idx]
        for lab in ("side_won_prev", "side_lost_prev"):
            same = (prevW[:, None] == np.array([0, 1])[None, :])
            c2 = same if lab == "side_won_prev" else (~same & (prevW[:, None] >= 0))
            ss = fil & c2[:, :, None] & dev[dix][:, None, None]
            if ss.sum():
                pw.append(dict(model=mn, cond=lab, n=int(ss.sum()), milli=round(float(pnl[ss].mean()), 2)))
    rep["prev_winner_split"] = pw

    # ---- F1 grid
    B = Book(days)
    meta = []
    for idx, (si, pi, ci) in enumerate(C1):
        if CANCEL[ci] <= S1[si]:
            continue
        px = PX[:, idx]
        for sc in SCALES:
            size = scale_sizes(sc, px)
            for mi, mn in enumerate(MODELS):
                qty = np.minimum(size, AV[:, idx, mi])
                for pm in PMAX:
                    q = np.where(px <= pm, qty, 0)
                    pnl = np.cumsum(q * (1000 * win - px), 2).sum(1)
                    reb = np.cumsum(q * REB * fee(px), 2).sum(1)
                    notl = np.cumsum(q * px, 2).sum(1)
                    for K in KS:
                        B.add(f"F1 s{S1[si]} st{STEP[pi]} c{CANCEL[ci]} {sc} K{K} pmax{pm} {mn}", dix, pnl[:, K - 1], reb[:, K - 1], notl[:, K - 1])
                        meta.append(("F1", mn))
    # ---- F2
    for ci, (tgt, emax, (rule, g), tstop, model) in enumerate(F2):
        x = F2a[:, ci]
        if np.isnan(x[:, 0]).all():
            continue
        x = np.nan_to_num(x)
        B.add(f"F2 tgt{tgt} e{emax} {rule}{g} stop{tstop} {MODELS[model]}", dix, x[:, 0], x[:, 1], x[:, 2])
        meta.append(("F2", MODELS[model]))
    # ---- F3 (taker: no rebate, notional is taker)
    for ci, (z, tf, n, s, A, cap) in enumerate(F3):
        x = F3a[:, ci]
        if np.isnan(x[:, 0]).all():
            continue
        x = np.nan_to_num(x)
        B.add(f"F3 z{z} t{tf} n{n} s{s} {'UP' if A == 0 else 'DN'} {'cap' if cap else 'nocap'}", dix, x[:, 0], np.zeros(M), x[:, 2])
        meta.append(("F3", "taker"))
    P, R, N = B.mats()
    PR = P + R
    fam = np.array([m_[0] for m_ in meta])
    mod = np.array([m_[1] for m_ in meta])
    names = np.array(B.names)
    dg = stats(P, N, dev, Cn)
    dr = stats(PR, N, dev, Cn)
    hg = stats(P, N, hold, Cn)
    hr = stats(PR, N, hold, Cn)
    minvol = 300 * BASE * 300  # >= ~300 markets of base-size fills (milli)
    nmk = np.array([r_[3] for r_ in B.risk])
    active = (N[:, dev].sum(1) >= minvol) & (nmk >= 300)
    ts = tstat(PR[:, dev], N[:, dev])
    summ = {}
    for f_ in ("F1", "F2", "F3"):
        for m_ in sorted(set(mod[fam == f_])):
            sel = (fam == f_) & (mod == m_) & active
            if not sel.any():
                continue
            i = np.flatnonzero(sel)
            b = i[np.argmax(ts[i])]
            summ[f"{f_}/{m_}"] = dict(
                configs=int(sel.sum()),
                dev_ci_lb_gt0_gross=int((dg[2][i] > 0).sum()),
                dev_ci_lb_gt0_reb=int((dr[2][i] > 0).sum()),
                naive_5pct=round(0.05 * sel.sum(), 1),
                dev_positive_reb=int((dr[1][i] > 0).sum()),
                best=names[b],
                best_dev=dict(usd=round(dr[0][b], 2), c_per_usd=round(dr[1][b], 3), ci=[round(dr[2][b], 3), round(dr[3][b], 3)], usd_day=round(dr[4][b], 2), vol_k_usd=round(dr[5][b], 1)),
                best_hold=dict(usd=round(hr[0][b], 2), c_per_usd=round(hr[1][b], 3), ci=[round(hr[2][b], 3), round(hr[3][b], 3)], usd_day=round(hr[4][b], 2)),
                best_risk=dict(worst_mkt_usd=round(B.risk[b][0] / 1000, 2), cost_p99_usd=round(B.risk[b][1] / 1000, 2), cost_max_usd=round(B.risk[b][2] / 1000, 2)),
                median_dev_c_per_usd_reb=round(float(np.median(dr[1][i])), 3),
            )
    rep["families"] = summ

    # ---- walk-forward (per family x model; dev+hold days, excluding BAD)
    keep = ~bad
    wfo = {}
    for key in summ:
        f_, m_ = key.split("/")
        sel = (fam == f_) & (mod == m_)
        op, on, picks = wf(PR[:, keep], N[:, keep], sel, minvol=minvol * 14 / max(dev.sum(), 1))
        Pk = op[None, :]
        Nk = on[None, :]
        tm = np.r_[np.zeros(14, bool), np.ones(keep.sum() - 14, bool)]
        s_ = stats(Pk, Nk, tm)
        wfo[key] = dict(usd=round(float(s_[0][0]), 2), c_per_usd=round(float(s_[1][0]), 3), ci=[round(float(s_[2][0]), 3), round(float(s_[3][0]), 3)], usd_day=round(float(s_[4][0]), 2), picks=[names[p] for p in picks[:3]] + (["..."] if len(picks) > 3 else []))
    rep["walk_forward"] = wfo

    # ---- volume cost (airdrop / rebate farming): highest maker volume per $ lost, dev
    vc = []
    for m_ in ("through", "touch", "level", "queue"):
        sel = np.flatnonzero((mod == m_) & (fam != "F3") & active)
        if not len(sel):
            continue
        vol_day = N[sel][:, dev].sum(1) / 1000 / dev.sum()
        cost = -dr[1][sel]  # cents lost per $ volume (with rebates)
        for lim in (0.25, 0.5, 1.0):
            c_ok = sel[cost <= lim]
            if not len(c_ok):
                continue
            j = c_ok[np.argmax(N[c_ok][:, dev].sum(1))]
            vc.append(dict(model=m_, max_cost_c_per_usd=lim, config=names[j], dev_vol_usd_day=round(float(N[j, dev].sum() / 1000 / dev.sum()), 1), dev_c_per_usd=round(float(dr[1][j]), 3), hold_c_per_usd=round(float(hr[1][j]), 3), hold_vol_usd_day=round(float(N[j, hold].sum() / 1000 / max(hold.sum(), 1)), 1)))
    rep["volume_cost"] = vc

    # ---- F4 cross-market sequence sizing on top dev picks per family/model
    f4 = []
    order_d = dix
    for key, v in summ.items():
        b = int(np.flatnonzero(names == v["best"])[0])
        f_, m_ = key.split("/")
        per = per_market(b, fam[b], names[b], PX, AV, F2a, F3a, win, M)
        if per is None:
            continue
        pnl, notl = per
        for rule, r_, L in (("mart", 2, 4), ("mart", 2, 6), ("mart", 3, 4), ("anti", 2, 3), ("dalembert", 1, 8)):
            mult = seq_mult(pnl, rule, r_, L)
            sp, sn = pnl * mult, notl * mult
            Pd = np.bincount(order_d, sp, nd)[None]
            Nd = np.bincount(order_d, sn, nd)[None]
            d_ = stats(Pd, Nd, dev, Cn)
            h_ = stats(Pd, Nd, hold, Cn)
            eq = np.cumsum(sp) / 1000
            ac = float(np.corrcoef(pnl[:-1], pnl[1:])[0, 1]) if pnl.std() > 0 else 0.0
            f4.append(dict(base=names[b], rule=f"{rule} r{r_} L{L}", pnl_lag1_corr=round(ac, 4), dev_c_per_usd=round(float(d_[1][0]), 3), dev_ci=[round(float(d_[2][0]), 3), round(float(d_[3][0]), 3)], hold_c_per_usd=round(float(h_[1][0]), 3), max_dd_usd=round(float((np.maximum.accumulate(eq) - eq).max()), 2), base_c_per_usd=round(float(100 * pnl[dev[dix]].sum() / max(notl[dev[dix]].sum(), 1)), 3)))
    rep["f4_sequence"] = f4
    # pnl autocorrelation of base unit (does a loss predict the next market?)
    rep["verdict"] = verdict(summ, wfo)
    (a.out / "report.json").write_text(json.dumps(rep, indent=1, default=float))
    pr(rep)


def per_market(b, f_, name, PX, AV, F2a, F3a, win, M):
    if f_ == "F1":
        tok = name.split()
        s, st, c = int(tok[1][1:]), int(tok[2][2:]), int(tok[3][1:])
        sc, K, pm, mn = tok[4], int(tok[5][1:]), int(tok[6][4:]), tok[7]
        idx = C1.index((S1.index(s), STEP.index(st), CANCEL.index(c)))
        px = PX[:, idx]
        q = np.minimum(scale_sizes(sc, px), AV[:, idx, MODELS.index(mn)])
        q = np.where(px <= pm, q, 0)[:, :, :K]
        p = px[:, :, :K]
        return (q * (1000 * win - p) + q * REB * fee(p)).sum((1, 2)), (q * p).sum((1, 2))
    if f_ == "F2":
        i = [f"F2 tgt{t} e{e} {r}{g} stop{s} {MODELS[m]}" for t, e, (r, g), s, m in F2].index(name)
        x = np.nan_to_num(F2a[:, i])
        return x[:, 0] + x[:, 1], x[:, 2]
    return None


def seq_mult(pnl, rule, r, L):
    mult = np.ones(len(pnl))
    lvl = 0
    for i in range(1, len(pnl)):
        lost = pnl[i - 1] < 0
        if rule == "mart":
            lvl = min(lvl + 1, L) if lost else 0
            mult[i] = r**lvl
        elif rule == "anti":
            lvl = min(lvl + 1, L) if (pnl[i - 1] > 0) else 0
            mult[i] = r**lvl
        else:
            lvl = min(lvl + 1, L) if lost else max(lvl - 1, 0)
            mult[i] = 1 + lvl
    return mult


def verdict(summ, wfo):
    out = {}
    for key, v in summ.items():
        h = v["best_hold"]
        w = wfo.get(key, {})
        ok_h = h["ci"][0] > 0 and h["usd_day"] >= 2.0
        ok_w = bool(w) and w["ci"][0] > 0
        out[key] = "PASS" if ok_h and ok_w else "FAIL"
    real = [v for k, v in out.items() if k.split("/")[1] in ("through", "touch", "level", "taker")]
    qonly = [v for k, v in out.items() if k.split("/")[1] == "queue"]
    if "PASS" in real:
        out["overall"] = "PASS: build a live maker shadow for the passing family"
    elif "PASS" in qonly:
        out["overall"] = "QUEUE-ONLY: profitable only if we sit at the front of the queue; needs a live fill test, not more backtests"
    else:
        out["overall"] = "FAIL: no scaling, sizing or sequencing rule survives, even with queue-front fills"
    return out


def pr(rep):
    print(f"{rep['markets']} markets / {rep['days']} days {rep['first']}..{rep['last']} (dev {rep['dev_days']}d, hold {rep['hold_days']}d)")
    print(f"P(outcome == previous outcome) = {rep['outcome_same_as_prev']}")
    print("\nATLAS (flat 1-share rung, cancel 300s, s=5, step 10/30): milli/share gross | +rebate")
    for r in rep["atlas"]:
        if r["s"] == 5 and r["step"] in (10, 30):
            print(f"  {r['model']:7} st{r['step']:>2} rungs {r['rungs']:>5} px {r['px']:>8} {r['split']:4} n={r['n']:>7} {r['milli']:>8} | {r['milli_reb']:>8}  se {r['se']}")
    print("\nprev-winner split:", rep["prev_winner_split"])
    print("\nFAMILIES (best = highest dev t-stat incl. rebates, >=300 markets with fills):")
    for k, v in rep["families"].items():
        print(f"  {k:12} n={v['configs']:>5} devCI>0 gross {v['dev_ci_lb_gt0_gross']} reb {v['dev_ci_lb_gt0_reb']} (naive 5% {v['naive_5pct']}) median {v['median_dev_c_per_usd_reb']}c/$")
        print(f"       best {v['best']}\n       dev {v['best_dev']}\n       hold {v['best_hold']}\n       risk {v['best_risk']}")
    print("\nWALK-FORWARD OOS (incl. rebates):")
    for k, v in rep["walk_forward"].items():
        print(f"  {k:12} {v['usd']:>10} usd {v['c_per_usd']:>7} c/$ CI {v['ci']} {v['usd_day']} usd/day")
    print("\nVOLUME COST (max maker volume at a given cost):")
    for r in rep["volume_cost"]:
        print(f"  {r}")
    print("\nF4 SEQUENCE SIZING:")
    for r in rep["f4_sequence"]:
        print(f"  {r}")
    print("\nVERDICT:", json.dumps(rep["verdict"], indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("scan")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--skip", default="")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    {"scan": cmd_scan, "report": cmd_report}[a.cmd](a)
