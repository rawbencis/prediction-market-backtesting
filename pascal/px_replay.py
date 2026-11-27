"""PX replay: Pascal x Polymarket rules on pascal_rec.py recordings (frozen/PX_plan.json).

cache  DAY     stream OUT/DAY/*.jsonl.gz once -> per-market numpy arrays under CACHE/DAY/ (npz + meta.json)
run    DAYS..  replay every PX cell on cached days -> attempts CSV + cell table (dev or holdout)
synth          synthetic checks (flat books, planted 3¢ gap 5 s, planted gap 100 ms)

Clocks. Pascal rows carry the matching engine's state_time_ms (st); our view of a row is st + LP, LP = that
day's median (recv - st) from the recorder (≈ 7 ms). Polymarket rows carry the exchange timestamp x; our view
is x + 116 ms. All times are ms on the (NTP-synced, < 1 µs) box clock.
Orders. A Pascal order sent at t enters the first 50 ms round starting at or after t + 2 ms (rounds on
multiples of 50 ms; phase unknown, so this is a convention) and matches against the book as published after
that round (last row with st < round + 50), i.e. after that round's cancels and earlier same-round orders.
A Polymarket order sent at t matches at t + PM_LAT (430 ms, sensitivity 1000) against the displayed top 3 at
that exchange time. Fills walk at most the recorded 3 levels.
Polymarket books: only the long token's book is used; the reverse token's ask = 1 - long bid (same size), as
in Polymarket's merged book (checked on the 11-27 sample: 92% of paired updates mirror exactly, the rest
differ only by the two messages' timing).
"""

import argparse
import csv
import gzip
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HOME = Path.home()
REC = HOME / "pascal-data" / "rec"
CACHE = HOME / "pascal-data" / "cache"
GAMMA_CENSUS = HOME / "pascal-data" / "raw" / "census" / "gamma.json.gz"
PM_VIEW = 116.0
ROUND = 50.0
SEND = 2.0
NL = 3  # levels
# row layout for both venues: t, b1p, b1s, b2p, b2s, b3p, b3s, a1p, a1s, a2p, a2s, a3p, a3s
W = 1 + 4 * NL


def loads(s):
    return json.loads(s)


# ---------------------------------------------------------------- cache


def day_files(rec, day):
    d = Path(rec) / day
    return sorted(d.glob("*.jsonl.gz"))


def row_of(t, b, a):
    r = [t] + [math.nan] * (W - 1)
    for i, (p, s) in enumerate(b[:NL]):
        r[1 + 2 * i], r[2 + 2 * i] = p, s
    for i, (p, s) in enumerate(a[:NL]):
        r[1 + 2 * NL + 2 * i], r[2 + 2 * NL + 2 * i] = p, s
    return r


def build_cache(day, rec=REC, cache=CACHE):
    """One streaming pass over the day's files. Day = UTC day of the receive hour files."""
    out = Path(cache) / day
    out.mkdir(parents=True, exist_ok=True)
    P = defaultdict(list)  # symbol -> rows (t = st)
    M = defaultdict(list)  # symbol -> rows (t = x), long token only
    T = defaultdict(list)  # symbol -> trades [ts, p, sz, taker_buy]
    wallets = defaultdict(list)  # symbol -> [(ts, maker, taker)]
    lagp, spec, hdr_syms = [], {}, {}
    seen_tid = set()
    for f in day_files(rec, day):
        with gzip.open(f, "rt") as fh:
            for ln in fh:
                try:
                    o = loads(ln)
                except ValueError:
                    continue  # truncated last line of a killed writer
                k = o["k"]
                if k == "pb":
                    if not o.get("st"):
                        continue
                    st = float(o["st"])
                    P[o["s"]].append(row_of(st, o["b"], o["a"]))
                    if o.get("y") == "u":
                        lagp.append(o["r"] - st)
                elif k == "mb":
                    s, leg = o["t"].rsplit("|", 1)
                    if leg == "L" and o.get("x"):
                        M[s].append(row_of(float(o["x"]), o["b"], o["a"]))
                elif k in ("pt", "pta"):
                    if o["id"] in seen_tid:
                        continue
                    seen_tid.add(o["id"])
                    T[o["s"]].append([float(o["ts"]), o["p"], o["sz"], 1.0 if o["side"] == "BID" else -1.0])
                    wallets[o["s"]].append((o["ts"], o.get("mk"), o.get("tk")))
                elif k == "gap":
                    g = o["r"]
                    nanrow = [g] + [math.nan] * (W - 1)
                    tgt = P if o["src"] == "pas" else M
                    for s in list(hdr_syms):
                        tgt[s].append(list(nanrow))
                elif k == "hdr":
                    for s, lt, rt in o["s"]:
                        hdr_syms[s] = (lt, rt)
                elif k == "pspec":
                    spec[o["s"]] = o["spec"]
    meta = {"day": day, "lag_pas_ms": float(np.median(lagp)) if lagp else 7.0, "n_lag": len(lagp),
            "symbols": sorted(set(P) | set(M)), "spec": spec, "tokens": hdr_syms}
    for s in meta["symbols"]:
        arrs = {}
        for name, src in (("P", P), ("M", M)):
            a = np.array(src.get(s) or np.zeros((0, W)), dtype=np.float64).reshape(-1, W)
            a = a[np.argsort(a[:, 0], kind="stable")] if len(a) else a
            arrs[name] = a
        tr = np.array(T.get(s) or np.zeros((0, 4)), dtype=np.float64).reshape(-1, 4)
        arrs["T"] = tr[np.argsort(tr[:, 0], kind="stable")] if len(tr) else tr
        np.savez_compressed(out / f"{safe(s)}.npz", **arrs)
    with open(out / "wallets.json", "w") as f:
        json.dump(wallets, f)
    with open(out / "meta.json", "w") as f:
        json.dump(meta, f)
    return meta


def safe(s):
    return s.replace("/", "_")


# ---------------------------------------------------------------- market metadata


def load_market_meta(rec=REC):
    """symbol -> dict(p_rate, rebate, ert (ms), start (ms), sports, event, is_mece, cond) from every saved
    /api/v1/markets snapshot (latest wins) plus the 11-27 raw snapshot."""
    out = {}
    paths = [HOME / "pascal-data/raw/snap/markets_20261127.json"] + sorted((Path(rec) / "markets").glob("*.json.gz"))
    for p in paths:
        if not p.exists():
            continue
        op = gzip.open if p.suffix == ".gz" else open
        with op(p, "rt") as f:
            d = json.load(f)
        for m in d["data"]:
            da = m.get("display_attributes") or {}
            ref = da.get("reference") or {}
            out[m["symbol"]] = dict(p_rate=float(m["taker_fee_rate"]), rebate=float(m["maker_rebate_share"]),
                                    ert=float(da.get("expected_resolution_time_ms") or 0),
                                    start=float(da.get("expected_event_start_time_ms") or 0),
                                    sports="Sports" in (da.get("tags") or ()), event=m["symbol"].split(".")[0],
                                    is_mece=bool(m.get("is_mece")), cond=ref.get("condition_id"),
                                    tags=da.get("tags") or [])
    return out


def load_pm_rates(path=GAMMA_CENSUS):
    if not Path(path).exists():
        return {}
    with gzip.open(path, "rt") as f:
        g = json.load(f)
    out = {}
    for c, m in g.items():
        fs = m.get("feeSchedule") or {}
        out[c] = float(fs.get("rate") or 0.0) if m.get("feesEnabled") else 0.0
    return out


# ---------------------------------------------------------------- book helpers


def at(arr, t):
    """Row index of the last row with time <= t, or -1."""
    return int(np.searchsorted(arr[:, 0], t, side="right")) - 1


def before(arr, t):
    """Row index of the last row with time < t."""
    return int(np.searchsorted(arr[:, 0], t, side="left")) - 1


def levels(row, outcome):
    """Levels to BUY `outcome` from one venue's long book row: [(cost per share, size, trade price)].
    long: the asks (cost p); short: the bids (cost 1 - p, traded at p)."""
    out = []
    if row is None:
        return out
    off = 1 + 2 * NL if outcome == "long" else 1
    for i in range(NL):
        p, s = row[off + 2 * i], row[off + 2 * i + 1]
        if p == p and s == s and s > 0:
            out.append(((p if outcome == "long" else 1 - p), s, p))
    return out


def take(row, outcome, qty, max_cost, rate):
    """Buy up to qty of outcome at cost <= max_cost per share. -> (filled, total cost incl. fee, fee)."""
    filled = cost = fee = 0.0
    for c, s, p in levels(row, outcome):
        if c > max_cost + 1e-12 or filled >= qty:
            break
        q = min(s, qty - filled)
        filled += q
        cost += q * c
        fee += q * rate * p * (1 - p)
    return filled, cost + fee, fee


def sell(row, outcome, qty, rate):
    """Sell qty of outcome at whatever the top 3 pay; the remainder beyond 3 levels at the 3rd level - 5¢
    (or 0 if the book is empty). -> proceeds net of fee."""
    other = "short" if outcome == "long" else "long"
    lv = levels(row, other)  # selling long = buying short at the bids: proceeds = 1 - cost
    got = 0.0
    left = qty
    last = None
    for c, s, p in lv:
        q = min(s, left)
        got += q * (1 - c) - q * rate * p * (1 - p)
        left -= q
        last = 1 - c
        if left <= 0:
            break
    if left > 0:
        got += left * max((last if last is not None else 0.05) - 0.05, 0.0)
    return got


def round_of(t):
    return math.ceil((t + SEND) / ROUND) * ROUND


class Mkt:
    def __init__(self, sym, cache_day, meta, mm, pm_rates):
        z = np.load(cache_day / f"{safe(sym)}.npz")
        self.sym, self.P, self.M, self.T = sym, z["P"], z["M"], z["T"]
        sp = (meta["spec"].get(sym) or {})
        info = mm.get(sym, {})
        self.p_rate = float(sp.get("taker_fee_rate") or info.get("p_rate", 0.02))
        self.rebate = float(sp.get("maker_rebate_share") or info.get("rebate", 0.25))
        self.m_rate = pm_rates.get(info.get("cond"), math.nan)
        self.info = info
        self.LP = meta["lag_pas_ms"]

    def prow(self, st_max, strict=False):
        i = before(self.P, st_max) if strict else at(self.P, st_max)
        return self.P[i] if i >= 0 else None

    def mrow(self, x_max):
        i = at(self.M, x_max)
        return self.M[i] if i >= 0 else None

    def p_after_round(self, R):
        return self.prow(R + ROUND, strict=True)


def in_play(info, t):
    return bool(info.get("sports")) and info.get("start", 0) > 0 and info["start"] <= t <= info["start"] + 4 * 3600e3


def primary(info, t):
    """Primary set: resolving within 30 days; sports excluded while in play."""
    ert = info.get("ert", 0)
    return ert > 0 and (ert - t) <= 30 * 86400e3 and not in_play(info, t)


# ---------------------------------------------------------------- PX-A


def px_a_events(mk, X, min_usd=25.0):
    """Candidate trigger times (view clock) where a pair costs < 1 - X after both taker fees with >= $25 on
    each leg's top level. -> list of (t, direction, p_cost, m_cost, shares)."""
    P, M = mk.P, mk.M
    if len(P) == 0 or len(M) == 0 or mk.m_rate != mk.m_rate:
        return []
    tv = np.union1d(P[:, 0] + mk.LP, M[:, 0] + PM_VIEW)
    ip = np.searchsorted(P[:, 0], tv - mk.LP, side="right") - 1
    im = np.searchsorted(M[:, 0], tv - PM_VIEW, side="right") - 1
    ok = (ip >= 0) & (im >= 0)
    tv, ip, im = tv[ok], ip[ok], im[ok]
    pr, mr = P[ip], M[im]
    rp, rm = mk.p_rate, mk.m_rate
    a = 1 + 2 * NL
    out = []
    # LP: Pascal long at Pascal ask + PM short (reverse token) at 1 - PM long bid
    pa, pas, mb, mbs = pr[:, a], pr[:, a + 1], mr[:, 1], mr[:, 2]
    c_lp = pa + rp * pa * (1 - pa) + (1 - mb) + rm * mb * (1 - mb)
    sz_lp = np.minimum(pas, mbs)
    ok_lp = (c_lp < 1 - X) & (pas * pa >= min_usd) & (mbs * (1 - mb) >= min_usd)
    # SP: Pascal short at 1 - Pascal bid + PM long at PM long ask
    pb, pbs, ma, mas = pr[:, 1], pr[:, 2], mr[:, a], mr[:, a + 1]
    c_sp = (1 - pb) + rp * pb * (1 - pb) + ma + rm * ma * (1 - ma)
    sz_sp = np.minimum(pbs, mas)
    ok_sp = (c_sp < 1 - X) & (pbs * (1 - pb) >= min_usd) & (mas * ma >= min_usd)
    for i in np.nonzero(ok_lp | ok_sp)[0]:
        d = "LP" if ok_lp[i] and (not ok_sp[i] or c_lp[i] <= c_sp[i]) else "SP"
        if d == "LP":
            out.append((float(tv[i]), d, float(pa[i]), float(1 - mb[i]), float(sz_lp[i])))
        else:
            out.append((float(tv[i]), d, float(1 - pb[i]), float(ma[i]), float(sz_sp[i])))
    return out


def px_a_exec(mk, ev, order, pm_lat, cap_usd=200.0):
    """Execute one PX-A attempt. -> dict(shares, pnl, locked, ...) in $."""
    t, d, pc_seen, mc_seen, sz = ev
    po, mo = ("long", "short") if d == "LP" else ("short", "long")  # Pascal outcome, PM outcome
    q = math.floor(min(sz, cap_usd / max(pc_seen + mc_seen, 1e-9)))
    if q < 1:
        return None
    rp, rm = mk.p_rate, mk.m_rate

    def pas_leg(t_send, qty, lim):
        R = round_of(t_send)
        row = mk.p_after_round(R)
        f, c, _ = take(row, po, qty, lim, rp)
        return f, c, R + ROUND + mk.LP  # fill known (view clock): round result published + lag

    def pm_leg(t_send, qty, lim):
        T = t_send + pm_lat
        row = mk.mrow(T)
        f, c, _ = take(row, mo, qty, lim, rm)
        return f, c, T + PM_VIEW

    lim_p, lim_m = pc_seen + 0.01, mc_seen + 0.01
    if order == "pascal_first":
        f1, c1, tk = pas_leg(t, q, lim_p)
        f2, c2, tk2 = pm_leg(tk, f1, lim_m) if f1 > 0 else (0.0, 0.0, tk)
        legs = [("P", f1, c1, tk), ("M", f2, c2, tk2)]
    elif order == "pm_first":
        f1, c1, tk = pm_leg(t, q, lim_m)
        f2, c2, tk2 = pas_leg(tk, f1, lim_p) if f1 > 0 else (0.0, 0.0, tk)
        legs = [("M", f1, c1, tk), ("P", f2, c2, tk2)]
    else:
        f1, c1, tk1 = pas_leg(t, q, lim_p)
        f2, c2, tk2 = pm_leg(t, q, lim_m)
        legs = [("P", f1, c1, tk1), ("M", f2, c2, tk2)]
    fP = next(x for x in legs if x[0] == "P")
    fM = next(x for x in legs if x[0] == "M")
    pairs = min(fP[1], fM[1])
    pnl = pairs - (fP[2] * (pairs / fP[1]) if fP[1] else 0) - (fM[2] * (pairs / fM[1]) if fM[1] else 0)
    # unwind the excess leg at the bid, after the same latency, fee charged
    unwound = 0.0
    for v, f, c, tk in (fP, fM):
        ex = f - pairs
        if ex <= 1e-9:
            continue
        unwound = ex
        avg = c / f
        t_known = max(fP[3], fM[3])
        if v == "P":
            R = round_of(t_known)
            proceeds = sell(mk.p_after_round(R), po, ex, rp)
        else:
            proceeds = sell(mk.mrow(t_known + pm_lat), mo, ex, rm)
        pnl += proceeds - avg * ex
    return dict(t=t, dir=d, shares=q, pairs=pairs, unwound=unwound, pnl=pnl,
                locked=fP[2] * (pairs / fP[1] if fP[1] else 0) + fM[2] * (pairs / fM[1] if fM[1] else 0),
                seen_gap=1 - pc_seen - mc_seen)  # before fees


def run_px_a(mk, X, order, pm_lat, cooldown=60e3, filt=primary):
    out, last = [], -1e18
    for ev in px_a_events(mk, X):
        if ev[0] - last < cooldown:
            continue
        if not filt(mk.info, ev[0]):
            continue
        r = px_a_exec(mk, ev, order, pm_lat)
        if r is None:
            continue
        last = ev[0]
        out.append(r)
    return out


# ---------------------------------------------------------------- PX-B


def tick_of(p, spec_min=0.001, sig=3):
    pp = min(p, 1 - p)
    if pp <= 0:
        return spec_min
    ps = math.floor(math.log10(pp)) + 1
    return max(10 ** (ps - sig), spec_min)


def run_px_b(mk, h, pm_lat, usd=50.0, filt=primary, move=0.01):
    """Pascal maker at PM mid +- h, pulled/requoted when the PM mid seen moves >= 1¢ (lands in the first round
    starting >= x + 116 + 2 ms); fills from recorded Pascal taker trades; each fill hedged on PM 430 ms after
    it is known. -> list of fills."""
    P, M, T = mk.P, mk.M, mk.T
    if len(M) == 0 or len(T) == 0 or len(P) == 0 or mk.m_rate != mk.m_rate:
        return []
    a = 1 + 2 * NL
    mid = (M[:, 1] + M[:, a]) / 2
    okm = (M[:, 2] > 0) & (M[:, a + 1] > 0) & (mid == mid)
    # quote schedule: list of (t_on (exchange, round start), bid, ask)
    sched = []
    qmid = None
    for i in np.nonzero(okm)[0]:
        m = mid[i]
        if qmid is None or abs(m - qmid) >= move - 1e-12:
            qmid = m
            t_on = round_of(M[i, 0] + PM_VIEW)
            sched.append((t_on, m - h, m + h))
    # books going unknown (gap rows) pull quotes
    for i in np.nonzero(~okm & np.isnan(M[:, 1]))[0]:
        sched.append((round_of(M[i, 0] + PM_VIEW), math.nan, math.nan))
    sched.sort()
    if not sched:
        return []
    st = np.array([s[0] for s in sched])
    fills = []
    rp, rm = mk.p_rate, mk.m_rate
    # per quote epoch remaining size per side: refilled on each requote and after a fill is known
    rem = {}
    for ts, px, sz, tb in T:
        j = int(np.searchsorted(st, ts, side="right")) - 1
        if j < 0:
            continue
        t_on, qb, qa = sched[j]
        if qb != qb:
            continue
        if not filt(mk.info, ts):
            continue
        # round to Pascal ticks: bid down, ask up
        tk = tick_of((qb + qa) / 2)
        qb = math.floor(qb / tk + 1e-9) * tk
        qa = math.ceil(qa / tk - 1e-9) * tk
        row = mk.prow(ts, strict=True)  # displayed book just before the trade (excludes us)
        if row is None or row[1] != row[1] and row[a] != row[a]:
            continue
        bb, ba = row[1], row[a]
        side = "bid" if tb < 0 else "ask"  # taker sells -> hits bids; taker buys -> lifts asks
        q = qb if side == "bid" else qa
        if not (0 < q < 1):
            continue
        # post-only: the quote must not have crossed the book when it was posted
        prow_on = mk.prow(t_on, strict=True)
        if prow_on is not None:
            if side == "bid" and prow_on[a] == prow_on[a] and q >= prow_on[a] - 1e-12:
                continue
            if side == "ask" and prow_on[1] == prow_on[1] and q <= prow_on[1] + 1e-12:
                continue
        if side == "bid":
            improving = not (bb == bb) or q > bb + 1e-12
            hit = (px <= q + 1e-12) if improving else (px < q - 1e-12)
        else:
            improving = not (ba == ba) or q < ba - 1e-12
            hit = (px >= q - 1e-12) if improving else (px > q + 1e-12)
        if not hit:
            continue
        shares_side = usd / (q if side == "bid" else (1 - q))
        key = (j, side)
        left = rem.get(key, shares_side)
        f = min(sz, left)
        if f < 1:
            continue
        rem[key] = left - f
        # hedge: Pascal long (we bought at our bid) -> buy PM short; Pascal short -> buy PM long
        po = "long" if side == "bid" else "short"
        mo = "short" if po == "long" else "long"
        T_h = ts + mk.LP + pm_lat
        hrow = mk.mrow(T_h)
        hf, hc, _ = take(hrow, mo, f, 2.0, rm)
        if hf < f:  # beyond the recorded 3 levels: 3rd level + 1¢ (or 1 - pascal cost + 5¢ adverse if none)
            lv = levels(hrow, mo)
            worst = (lv[-1][0] + 0.01) if lv else 1.0
            hc += (f - hf) * worst
        p_cost = q if po == "long" else 1 - q
        pnl = f * (1 - p_cost) - hc
        reb = f * rp * mk.rebate * q * (1 - q)
        # markout of the Pascal fill vs PM mid seen 1 s / 5 s later (adverse selection)
        mo1 = mk.mrow(ts + 1000 - PM_VIEW)
        m1 = (mo1[1] + mo1[a]) / 2 if mo1 is not None else math.nan
        mark = (m1 - q) if po == "long" else (q - m1)
        fills.append(dict(t=ts, side=side, q=q, shares=f, pnl=pnl, rebate=reb, locked=f * p_cost + hc,
                          improving=improving, markout1s=mark, hedge_cost=hc / f))
    return fills


# ---------------------------------------------------------------- PX-C (MECE basket on Pascal)


def run_px_c(mks, X, cap_usd=200.0, cooldown=60e3, min_usd=25.0, filt=primary):
    """mks: the markets of one MECE event (all outcomes). Buy every outcome when the sum of asks + fees < 1-X."""
    if len(mks) < 2 or any(len(m.P) == 0 for m in mks):
        return []
    a = 1 + 2 * NL
    tv = np.unique(np.concatenate([m.P[:, 0] + m.LP for m in mks]))
    rows = [m.P[np.clip(np.searchsorted(m.P[:, 0], tv - m.LP, side="right") - 1, 0, None)] for m in mks]
    have = np.all([np.searchsorted(m.P[:, 0], tv - m.LP, side="right") - 1 >= 0 for m in mks], axis=0)
    cost = np.zeros(len(tv))
    okz = have.copy()
    for m, r in zip(mks, rows):
        pa, ps = r[:, a], r[:, a + 1]
        cost += pa + m.p_rate * pa * (1 - pa)
        okz &= (ps * pa >= min_usd)
    trig = np.nonzero(okz & (cost < 1 - X))[0]
    out, last = [], -1e18
    for i in trig:
        t = float(tv[i])
        if t - last < cooldown or not filt(mks[0].info, t):
            continue
        seen = [r[i] for r in rows]
        q = math.floor(min(min(s[a + 1] for s in seen), cap_usd / max(cost[i], 1e-9)))
        if q < 1:
            continue
        R = round_of(t)
        got = []
        for m, s in zip(mks, seen):
            f, c, _ = take(m.p_after_round(R), "long", q, s[a] + 0.01, m.p_rate)
            got.append((f, c))
        sets = min(f for f, _ in got)
        pnl = sets * 1.0
        for (f, c), m in zip(got, mks):
            pnl -= c * (sets / f) if f else 0
            ex = f - sets
            if ex > 1e-9:
                pnl += sell(m.p_after_round(round_of(R + ROUND + m.LP)), "long", ex, m.p_rate) - (c / f) * ex
        out.append(dict(t=t, shares=q, pairs=sets, pnl=pnl, locked=sets * cost[i], seen_gap=1 - cost[i]))
        last = t
    return out


# ---------------------------------------------------------------- driver


def cells():
    c = []
    for X in (0.02, 0.03, 0.04, 0.06):
        for order in ("pascal_first", "pm_first", "together"):
            c.append(("A", f"A_X{int(X * 100)}_{order}", dict(X=X, order=order)))
    for h in (0.01, 0.02, 0.03):
        c.append(("B", f"B_h{int(h * 100)}", dict(h=h)))
    for X in (0.01, 0.02, 0.04):
        c.append(("C", f"C_X{int(X * 100)}", dict(X=X)))
    return c


def replay_days(days, pm_lat=430.0, cache=CACHE, rec=REC, set_name="primary"):
    mm = load_market_meta(rec)
    pmr = load_pm_rates()
    filt = primary if set_name == "primary" else (lambda info, t: in_play(info, t))
    rows = []
    for day in days:
        cd = Path(cache) / day
        meta = json.loads((cd / "meta.json").read_text())
        mks = {}
        for s in meta["symbols"]:
            mks[s] = Mkt(s, cd, meta, mm, pmr)
        events = defaultdict(list)
        for s, m in mks.items():
            if m.info.get("is_mece"):
                events[m.info["event"]].append(m)
        for fam, name, kw in cells():
            if fam == "A":
                for s, m in mks.items():
                    for r in run_px_a(m, kw["X"], kw["order"], pm_lat, filt=filt):
                        rows.append(dict(cell=name, day=day, market=s, **r, days_to_res=(m.info.get("ert", 0) - r["t"]) / 86400e3))
            elif fam == "B":
                for s, m in mks.items():
                    for r in run_px_b(m, kw["h"], pm_lat, filt=filt):
                        rows.append(dict(cell=name, day=day, market=s, **r, days_to_res=(m.info.get("ert", 0) - r["t"]) / 86400e3))
            else:
                for e, ms in events.items():
                    for r in run_px_c(ms, kw["X"], filt=filt):
                        rows.append(dict(cell=name, day=day, market=e, **r, days_to_res=(ms[0].info.get("ert", 0) - r["t"]) / 86400e3))
        print(f"[{day}] {len(mks)} markets, rows so far {len(rows)}", flush=True)
    return rows


def boot_lb(rows, key, n=2000, seed=7, alpha=0.10):
    """90% lower bound (5th pct) of sum(pnl)/sum(shares) resampling `key` groups."""
    g = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        g[r[key]][0] += r["pnl"]
        g[r[key]][1] += r["shares"]
    v = np.array(list(g.values()))
    if len(v) < 2:
        return math.nan
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), size=(n, len(v)))
    s = v[idx].sum(axis=1)
    est = s[:, 0] / np.maximum(s[:, 1], 1e-9)
    return float(np.quantile(est, alpha / 2))


def summarize(rows, ndays):
    by = defaultdict(list)
    for r in rows:
        by[r["cell"]].append(r)
    out = []
    for fam, name, _ in cells():
        rs = by.get(name, [])
        sh = sum(r["shares"] for r in rs)
        pnl = sum(r["pnl"] for r in rs)
        reb = sum(r.get("rebate", 0.0) for r in rs)
        lbm, lbd = boot_lb(rs, "market"), boot_lb(rs, "day")
        lb = min(x for x in (lbm, lbd, math.inf) if x == x) if rs else math.nan
        dd = sorted(r["days_to_res"] for r in rs)
        out.append(dict(cell=name, attempts=len(rs), per_day=len(rs) / max(ndays, 1), markets=len({r["market"] for r in rs}),
                        shares=sh, c_per_share=100 * pnl / sh if sh else math.nan, lb_market=100 * lbm if lbm == lbm else math.nan,
                        lb_day=100 * lbd if lbd == lbd else math.nan, lb=100 * lb if lb == lb and lb != math.inf else math.nan,
                        usd_per_day=pnl / max(ndays, 1), rebate_c_per_share=100 * reb / sh if sh else math.nan,
                        locked_usd=sum(r["locked"] for r in rs), med_days_to_res=dd[len(dd) // 2] if dd else math.nan,
                        unwound_share=sum(r.get("unwound", 0) for r in rs) / sh if sh else math.nan))
    return out


def md_table(summ):
    h = ("| cell | attempts | /day | markets | shares | ¢/share | LB90 market | LB90 day | $/day | rebate ¢/sh | "
         "$ locked | med days to res | unwound share |")
    lines = [h, "|" + "---|" * 13]
    for s in summ:
        lines.append(f"| {s['cell']} | {s['attempts']} | {s['per_day']:.1f} | {s['markets']} | {s['shares']:.0f} | "
                     f"{s['c_per_share']:.2f} | {s['lb_market']:.2f} | {s['lb_day']:.2f} | {s['usd_per_day']:.2f} | "
                     f"{s['rebate_c_per_share']:.2f} | {s['locked_usd']:.0f} | {s['med_days_to_res']:.1f} | "
                     f"{s['unwound_share']:.2f} |")
    return "\n".join(lines)


def pick(summ, min_per_day=10.0):
    ok = [s for s in summ if s["per_day"] >= min_per_day and s["lb"] == s["lb"]]
    return max(ok, key=lambda s: s["lb"]) if ok else None


def passes(s, ndays):
    return bool(s and s["c_per_share"] >= 2.0 and s["lb"] > 0 and s["attempts"] >= 50 and s["markets"] >= 10
                and s["usd_per_day"] >= 10.0)


# ---------------------------------------------------------------- synthetic


def _synth_day(root, kind, sym="SYN_EVT.A", day="2030-01-01"):
    """Write a fake recorder day: flat books on both venues at 0.49/0.51 (Pascal) and 0.49/0.51 (PM), with a
    planted gap. kind: flat | pm5s | pm100ms | pas100ms."""
    d = Path(root) / "rec" / day
    d.mkdir(parents=True, exist_ok=True)
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000 + 3600e3
    lines = [{"k": "hdr", "r": t0, "gen": 1, "s": [[sym, "L1", "R1"]]},
             {"k": "pspec", "r": t0, "s": sym, "spec": {"taker_fee_rate": "0.02", "maker_rebate_share": "0.25"}}]
    flatP = ([[0.49, 1000], [0.48, 1000], [0.47, 1000]], [[0.51, 1000], [0.52, 1000], [0.53, 1000]])
    flatM = ([[0.49, 1000], [0.48, 1000], [0.47, 1000]], [[0.51, 1000], [0.52, 1000], [0.53, 1000]])
    for i in range(0, 3600, 5):  # a refresh row every 5 s for an hour
        t = t0 + i * 1000
        lines.append({"k": "pb", "r": t + 7, "s": sym, "st": t, "y": "u", "b": flatP[0], "a": flatP[1]})
        lines.append({"k": "mb", "r": t + 116, "t": sym + "|L", "x": t, "b": flatM[0], "a": flatM[1], "y": "p"})
    tg = t0 + 1800e3 + 3
    if kind in ("pm5s", "pm100ms"):
        dur = 5000 if kind == "pm5s" else 100
        # PM long bid jumps to 0.53 (reverse ask 0.47): Pascal ask 0.51 + 0.47 = 0.98 -> 2¢ before fees ... make 3¢:
        # PM long bid 0.545 -> reverse ask 0.455; Pascal ask 0.51 + fee 0.005 + 0.455 (PM fee 0) = 0.97: a 3¢ gap
        bad = [[0.545, 1000], [0.53, 1000], [0.52, 1000]]
        lines.append({"k": "mb", "r": tg + 116, "t": sym + "|L", "x": tg, "b": bad, "a": [[0.555, 1000], [0.56, 1000], [0.57, 1000]], "y": "p"})
        lines.append({"k": "mb", "r": tg + dur + 116, "t": sym + "|L", "x": tg + dur, "b": flatM[0], "a": flatM[1], "y": "p"})
    if kind == "pas100ms":
        lines.append({"k": "pb", "r": tg + 7, "s": sym, "st": tg, "y": "u", "b": flatP[0],
                      "a": [[0.46, 1000], [0.47, 1000], [0.48, 1000]]})
        lines.append({"k": "pb", "r": tg + 107, "s": sym, "st": tg + 100, "y": "u", "b": flatP[0], "a": flatP[1]})
    lines.sort(key=lambda o: o["r"])
    with gzip.open(d / "01.jsonl.gz", "wt") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    return day, t0


def synth(tmp):
    """Synthetic checks. Returns {case: summary rows of PX-A X=2¢ cells}."""
    res = {}
    import pascal_rec  # noqa: F401  (same directory; keeps the module importable in tests)

    for kind in ("flat", "pm5s", "pm100ms", "pas100ms"):
        root = Path(tmp) / kind
        day, t0 = _synth_day(root, kind)
        build_cache(day, rec=root / "rec", cache=root / "cache")
        meta = json.loads((root / "cache" / day / "meta.json").read_text())
        info = dict(ert=t0 + 5 * 86400e3, sports=False, start=0, event="SYN_EVT", is_mece=False, cond="c")
        mm = {"SYN_EVT.A": dict(info, p_rate=0.02, rebate=0.25)}
        mk = Mkt("SYN_EVT.A", root / "cache" / day, meta, mm, {"c": 0.0})
        res[kind] = {o: run_px_a(mk, 0.02, o, 430.0) for o in ("pascal_first", "pm_first", "together")}
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache")
    c.add_argument("days", nargs="+")
    r = sub.add_parser("run")
    r.add_argument("days", nargs="+")
    r.add_argument("--pm-lat", type=float, default=430.0)
    r.add_argument("--set", default="primary", choices=["primary", "inplay"])
    r.add_argument("--tag", required=True, help="dev | holdout | sens ...")
    r.add_argument("--out", default="reports/tokyo/px")
    s = sub.add_parser("synth")
    s.add_argument("--tmp", default=os.environ.get("TMPDIR", "/tmp") + "/px_synth")
    a = p.parse_args(argv)
    if a.cmd == "cache":
        for d in a.days:
            m = build_cache(d)
            print(d, len(m["symbols"]), "markets, Pascal lag", round(m["lag_pas_ms"], 2), "ms", flush=True)
    elif a.cmd == "run":
        rows = replay_days(a.days, a.pm_lat, set_name=a.set)
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{a.tag}_{a.set}_pm{int(a.pm_lat)}"
        if rows:
            keys = sorted({k for r in rows for k in r})
            with open(out / f"{stem}_attempts.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(rows)
        summ = summarize(rows, len(a.days))
        md = md_table(summ)
        pk = pick(summ)
        (out / f"{stem}_cells.md").write_text(md + "\n")
        with open(out / f"{stem}_cells.json", "w") as f:
            json.dump(summ, f, indent=1)
        print(md)
        print("pick (highest LB90, >= 10 attempts/day):", pk["cell"] if pk else None)
    elif a.cmd == "synth":
        res = synth(a.tmp)
        for k, v in res.items():
            for o, rs in v.items():
                print(k, o, len(rs), "attempts", round(sum(r["pnl"] for r in rs), 3), "$",
                      [round(r["seen_gap"] * 100, 2) for r in rs][:3])


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
