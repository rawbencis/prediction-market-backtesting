"""PF1 simulator: triggers, the Polymarket-first attempt (Dublin), the Lighter-first probe replay (Tokyo, V0) and the
frozen Lighter-first 500+500 rule (V2). See engine.py for clocks, fills and accounting.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from engine import (Book, MIN_SZ, Side, ViewIdx, fl, l_trade, load_lighter, load_pm_hour, pm_fill, pm_side, vwap)

START_OFFSET_MS = 5000.0
END_GUARD_MS = 1250.0  # frozen: trigger + 1 s <= window end - 0.25 s


def day_epoch(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def shift(day, n):
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


@dataclass
class Win:
    e: int
    rate: float
    pm: Side
    lt: Side
    up_won: object  # True / False / None (unknown)
    rate_src: str = ""
    cache: dict = field(default_factory=dict)

    @property
    def end(self):
        return (self.e + 900) * 1000.0

    def view(self, venue, lag):
        k = (venue, lag)
        if k not in self.cache:
            self.cache[k] = ViewIdx(self.pm if venue == "pm" else self.lt, lag)
        return self.cache[k]


def iter_windows(day, static, only=None):
    """Windows starting on UTC `day` with both books. Polymarket: hour chunks h-1, h; Lighter: day-1, day, day+1."""
    d0 = day_epoch(day)
    want = {e for e in range(d0, d0 + 86400, 900)} if only is None else set(only)
    lw = {}
    for dd in (shift(day, -1), day, shift(day, 1)):
        for e, s in load_lighter(dd, want).items():
            if e in lw:  # concatenate rows of the same window from two day files
                a = lw[e]
                lw[e] = Side(np.r_[a.tx, s.tx], np.r_[a.bp, s.bp], np.r_[a.bs, s.bs], np.r_[a.ap, s.ap], np.r_[a.as_, s.as_],
                             view=np.r_[a.view_raw, s.view_raw])
            else:
                lw[e] = s
    prev = load_pm_hour(shift(day, -1), 23)
    for hh in range(24):
        cur = load_pm_hour(day, hh)
        for q in range(4):
            e = d0 + hh * 3600 + q * 900
            if e not in want or cur is None:
                continue
            pm = pm_side([prev, cur], e)
            lt = lw.get(e)
            if pm is None or lt is None:
                continue
            rate, src = static.rate(e)
            yield Win(e, rate, pm, lt, static.out.get(e), src)
        prev = cur


# ------------------------------------------------------------------ book helpers
def tops(side, idx):
    """Best bid/ask/sizes arrays for indices (idx < 0 -> NaN)."""
    ok = idx >= 0
    j = np.where(ok, idx, 0)
    bb, ba = side.bp[j, 0], side.ap[j, 0]
    bs, as_ = side.bs[j, 0], side.as_[j, 0]
    bb = np.where(ok, bb, np.nan)
    ba = np.where(ok, ba, np.nan)
    return bb, ba, bs, as_


def edges(w, lb, la, pb, pa):
    fpm = lambda p: w.rate * p * (1 - p)  # noqa: E731
    ea = 1 - (la + (1 - pb) + fl(la) + fpm(1 - pb))
    eb = 1 - ((1 - lb) + pa + fl(lb) + fpm(pa))
    return ea, eb


def book_ok(bb, ba):
    return (bb > 0) & (ba > bb)


@dataclass
class Trig:
    t: float
    side: str  # a: Lighter buy Up + PM buy Down; b: Lighter sell Up + PM buy Up
    edge: float
    lpx: float
    lsz: float
    ppx: float  # PM hedge token ask as seen
    psz: float


def triggers(w, pm_lag, l_lag, th, pause_ms=5000.0, strict=False):
    """PF1 triggers: every view update, edge >= th after both fees, min top sizes >= 5, both books valid,
    from start + 5 s to end - 1.25 s, then pause_ms after each trigger (any side)."""
    vp, vl = w.view("pm", pm_lag), w.view("lt", l_lag)
    t0, t1 = w.e * 1000.0 + START_OFFSET_MS, w.end - END_GUARD_MS
    T = np.union1d(vp.v[(vp.v >= t0) & (vp.v <= t1)], vl.v[(vl.v >= t0) & (vl.v <= t1)])
    if len(T) == 0:
        return []
    ip = np.searchsorted(vp.v, T, "right") - 1
    il = np.searchsorted(vl.v, T, "right") - 1
    pb, pa, pbs, pas = tops(w.pm, ip)
    lb, la, lbs, las = tops(w.lt, il)
    with np.errstate(invalid="ignore"):
        ea, eb = edges(w, lb, la, pb, pa)
        ok = book_ok(pb, pa) & book_ok(lb, la)
        cmp = (lambda x: x > th + 1e-9) if strict else (lambda x: x >= th - 1e-9)
        ma = ok & cmp(ea) & (np.minimum(las, pbs) >= MIN_SZ)
        mb = ok & cmp(eb) & (np.minimum(lbs, pas) >= MIN_SZ)
    out, nxt = [], -1.0
    for k in np.flatnonzero(ma | mb):
        t = T[k]
        if t < nxt:
            continue
        # if both sides fire at once, take the larger edge
        if ma[k] and (not mb[k] or ea[k] >= eb[k]):
            out.append(Trig(t, "a", float(ea[k]), float(la[k]), float(las[k]), float(1 - pb[k]), float(pbs[k])))
        else:
            out.append(Trig(t, "b", float(eb[k]), float(lb[k]), float(lbs[k]), float(pa[k]), float(pas[k])))
        nxt = t + pause_ms
    return out


# ------------------------------------------------------------------ PF1 attempt
@dataclass
class Lat:
    """Latencies (ms). Dublin defaults; the Tokyo variant is used only in the synthetic check.
    Amendment 2026-11-27 (measured, not fitted): Polymarket matched at arrival in London + 320 ms (live ack p50 ~435 ms
    from Tokyo - ~115 ms Tokyo->London); Lighter matched at arrival + 158 ms (Tokyo send -> exchange tx 161 ms, less
    ~3 ms Tokyo->Lighter); competitor term on (engine.pm_fill)."""
    pm_send: float = 2.0  # trigger -> order leaves
    pm_oneway: float = 5.0  # box -> Polymarket matching engine
    pm_hold: float = 320.0  # Polymarket: arrival -> match (primary; sensitivity 250, 400)
    pm_back: float = 5.0  # Polymarket -> box (fill known)
    l_oneway: float = 100.0  # box -> Lighter (Tokyo -> Dublin ~100 ms one way)
    l_hold: float = 158.0  # Lighter: arrival -> match
    comp: bool = True  # Polymarket competitor term
    l_back: float = 100.0
    after_pm_ms: float = 10.0  # (a): Lighter order sent at PM match + this
    unwind_gap: float = 50.0  # PM unwind sent at Lighter-no-fill known + this


def pf1_attempt(w, tr, L, M, timing, q, lat, unwind="sell", stats=None):
    """One Polymarket-first attempt. -> dict with fills, unwinds and P&L (settled at the window's outcome)."""
    bk = Book(w.rate)
    a = tr.side == "a"
    token = "down" if a else "up"
    buy_up = a  # Lighter leg: side a buys Up at the ask, side b sells Up at the bid
    pm_lim = min(0.99, tr.ppx + L / 100)
    l_lim = tr.lpx + M / 100 if a else tr.lpx - M / 100
    pm_arr = tr.t + lat.pm_send + lat.pm_oneway
    pm_match = pm_arr + lat.pm_hold
    f_pm, pf = (0.0, [])
    if pm_match <= w.end:
        f_pm, pf = pm_fill(w.pm, token, True, pm_lim, q, pm_arr, pm_match, lat.comp, stats)
    bk.pm(token, pf)
    r = dict(e=w.e, t=tr.t, side=tr.side, edge=tr.edge, q=q, f_pm=f_pm, pm_px=vwap(pf), f_l=0.0, l_px=float("nan"), l_sent=False,
             unwind_usd=0.0, unwound=0.0, held=0.0, late=(w.end - tr.t) <= 120000)
    if timing == "a":
        l_size = f_pm if f_pm >= MIN_SZ else 0.0
        l_match = pm_match + lat.after_pm_ms + lat.l_oneway + lat.l_hold
    else:
        l_size = q
        l_match = tr.t + lat.pm_send + lat.l_oneway + lat.l_hold
    f_l, lfills = 0.0, []
    if l_size > 0:
        r["l_sent"] = True
        if l_match <= w.end:
            f_l, lfills = l_trade(w.lt, w.lt.at(l_match), buy_up, l_lim, l_size)
    bk.lighter(buy_up, lfills)
    r["f_l"], r["l_px"] = f_l, vwap(lfills)
    # mismatched legs
    ex_pm, ex_l = f_pm - f_l, f_l - f_pm
    if ex_pm > 1e-9:
        if unwind == "sell" and ex_pm >= MIN_SZ:
            known = (l_match + lat.l_back) if l_size > 0 else (pm_match + lat.pm_back)
            ua = known + lat.unwind_gap + lat.pm_oneway
            um = ua + lat.pm_hold
            if um <= w.end:
                got, sf = pm_fill(w.pm, token, False, 0.0, ex_pm, ua, um, lat.comp)
                basis = sum(p * s for p, s in pf) / f_pm  # PM buy price per share; its fee per share below
                fee_buy = w.rate * sum(p * (1 - p) * s for p, s in pf) / f_pm
                cash0 = bk.cash
                bk.pm(token, sf, sell=True)
                r["unwind_usd"] += (bk.cash - cash0) - got * (basis + fee_buy)
                r["unwound"] += got
                ex_pm -= got
        r["held"] += max(ex_pm, 0.0)
    elif ex_l > 1e-9:
        if unwind == "sell" and ex_l >= MIN_SZ:
            known = max(pm_match + lat.pm_back, l_match + lat.l_back)
            um = known + lat.l_oneway + lat.l_hold
            if um <= w.end:
                # close: if we bought Up, sell Up at the bid; if we sold Up, buy Up back at the ask
                got, cf = l_trade(w.lt, w.lt.at(um), not buy_up, 0.0 if buy_up else 1.0, ex_l)
                basis = sum((p if buy_up else 1 - p) * s for p, s in lfills) / f_l
                fee_in = sum(fl(p) * s for p, s in lfills) / f_l
                cash0 = bk.cash
                bk.lighter_close(buy_up, cf)
                r["unwind_usd"] += (bk.cash - cash0) - got * (basis + fee_in)
                r["unwound"] += got
                ex_l -= got
        r["held"] += max(ex_l, 0.0)
    r["pair"] = min(f_pm, f_l)
    r["pnl"] = bk.settle(w.up_won) if w.up_won is not None else float("nan")
    r["fees"] = bk.fees
    # P&L of the paired shares alone (1 - both prices - both fees, per share), for reporting
    if r["pair"] > 0:
        lp = r["l_px"] if a else 1 - r["l_px"]
        r["pair_cps"] = 100 * (1 - lp - r["pm_px"] - fl(r["l_px"]) - w.rate * r["pm_px"] * (1 - r["pm_px"]))
    else:
        r["pair_cps"] = float("nan")
    return r


# ------------------------------------------------------------------ V0: the live probe as it ran (Tokyo, Lighter first)
@dataclass
class ProbeLat:
    l_view: float = 2.0
    pm_view: float = 112.0
    persist_ms: float = 250.0
    l_send: float = 500.0
    l_hold: float = 161.0  # Lighter matched at send + 161 (measured send -> exchange tx)
    l_known: float = 330.0  # Lighter fill known at Tokyo after send (measured median 330-335)
    pm_after_lfill: float = 1.6  # measured median 1.5-1.6
    pm_oneway: float = 115.0  # Tokyo -> London
    pm_hold: float = 320.0  # arrival -> match (live ack p50 ~435 - 115)
    comp: bool = True
    pm_retry: float = 420.0
    pm_attempts: int = 4  # live max: 4 attempts (1 + 3 retries)
    pm_over: float = 0.03
    pm_known: float = 470.0  # PM result known at Tokyo after send (measured median ack 434-474)
    unwind_px: float = 0.05


def probe_replay(w, t0, side, lpx, ppx_trig, q, plat, stats=None):
    """Simulate one probe execution from its trigger at t0 (Tokyo local ms): persistence, Lighter IOC, PM FAK with
    retries, unwind on Lighter. The trigger itself is the probe's (same id)."""
    a = side == "a"
    token = "down" if a else "up"
    vp, vl = w.view("pm", plat.pm_view), w.view("lt", plat.l_view)
    # persistence at +250 (Tokyo views)
    ip, il = vp.at(t0 + plat.persist_ms), vl.at(t0 + plat.persist_ms)
    persist = False
    if w.pm.valid(ip) and w.lt.valid(il):
        ea, eb = edges(w, w.lt.bp[il, 0], w.lt.ap[il, 0], w.pm.bp[ip, 0], w.pm.ap[ip, 0])
        persist = (ea if a else eb) >= 0.04 - 1e-9
    # displayed at +500 (Tokyo view)
    il5 = vl.at(t0 + 500)
    disp = False
    if w.lt.valid(il5):
        p, s = (w.lt.ap[il5, 0], w.lt.as_[il5, 0]) if a else (w.lt.bp[il5, 0], w.lt.bs[il5, 0])
        disp = s >= MIN_SZ and (p <= lpx + 1e-9 if a else p >= lpx - 1e-9)
    ls = t0 + plat.l_send
    lm = ls + plat.l_hold
    f_l, lf = l_trade(w.lt, w.lt.at(lm), a, lpx, q) if lm <= w.end else (0.0, [])
    r = dict(e=w.e, t=t0, side=side, persist=persist, disp500=disp, f_l=f_l, l_px=vwap(lf), f_pm=0.0, pm_px=float("nan"), attempts=0,
             unwound=0.0, unwind_usd=0.0)
    if f_l <= 0:
        return r
    rem, pf = f_l, []
    send = ls + plat.l_known + plat.pm_after_lfill
    last_send = send
    for k in range(plat.pm_attempts):
        if rem < 1e-9 or (pf and rem < MIN_SZ):
            break
        s_k = send + k * plat.pm_retry
        iv = vp.at(s_k)
        if not w.pm.valid(iv):
            continue
        ask = (1 - w.pm.bp[iv, 0]) if a else w.pm.ap[iv, 0]
        lim = min(0.99, ask + plat.pm_over)
        m = s_k + plat.pm_oneway + plat.pm_hold
        r["attempts"] = k + 1
        last_send = s_k
        if m > w.end:
            break
        got, f = pm_fill(w.pm, token, True, lim, rem, s_k + plat.pm_oneway, m, plat.comp, stats)
        pf += f
        rem -= got
    r["f_pm"], r["pm_px"] = f_l - rem, vwap(pf)
    if r["f_pm"] > 0:
        lp = r["l_px"] if a else 1 - r["l_px"]
        r["pair_cps"] = 100 * (1 - lp - r["pm_px"] - fl(r["l_px"]) - w.rate * sum(p * (1 - p) * s for p, s in pf) / r["f_pm"])
        r["slip"] = 100 * (r["pm_px"] - ppx_trig)
    if rem >= MIN_SZ - 1e-9 and r["f_pm"] == 0:  # unwind on Lighter at best -/+ 5c
        um = last_send + plat.pm_known + plat.l_hold
        il = w.lt.at(um)
        if w.lt.valid(il) and um <= w.end:
            if a:
                got, cf = l_trade(w.lt, il, False, w.lt.bp[il, 0] - plat.unwind_px, rem)
                r["unwind_usd"] = sum((p - fl(p)) * s for p, s in cf) - got * (r["l_px"] + fl(r["l_px"]))
            else:
                got, cf = l_trade(w.lt, il, True, w.lt.ap[il, 0] + plat.unwind_px, rem)
                r["unwind_usd"] = sum((r["l_px"] - p - fl(p)) * s for p, s in cf) - got * fl(r["l_px"])
            r["unwound"] = got
    return r


# ------------------------------------------------------------------ V2: frozen Lighter-first 500 + 500 (Tokyo-like local timeline)
def frozen_500(w, l_lag=None, pm_lag=112.0, th=0.04, grid_ms=250.0):
    """Frozen t1 rule on a 250 ms grid: Lighter fill at +500 if quote still >= 5 at the trigger price or better (fill at the
    then-best, qty = its size), PM hedge at +1000 at the prevailing top price, clip min(Lighter size, PM size, 200)."""
    vp, vl = w.view("pm", pm_lag), w.view("lt", l_lag)
    t0, t1 = w.e * 1000.0 + START_OFFSET_MS, w.end - END_GUARD_MS
    G = np.arange(math.ceil(t0 / grid_ms) * grid_ms, t1 + 1e-9, grid_ms)
    ip, il = np.searchsorted(vp.v, G, "right") - 1, np.searchsorted(vl.v, G, "right") - 1
    pb, pa, pbs, pas = tops(w.pm, ip)
    lb, la, lbs, las = tops(w.lt, il)
    with np.errstate(invalid="ignore"):
        ea, eb = edges(w, lb, la, pb, pa)
        ok = book_ok(pb, pa) & book_ok(lb, la)
        ma = ok & (ea > th) & (np.minimum(las, pbs) >= MIN_SZ)
        mb = ok & (eb > th) & (np.minimum(lbs, pas) >= MIN_SZ)
    nxt = {"a": -1.0, "b": -1.0}
    out = []
    for k in np.flatnonzero(ma | mb):
        for side, m in (("a", ma), ("b", mb)):
            t = G[k]
            if not m[k] or t < nxt[side]:
                continue
            a = side == "a"
            lpx = la[k] if a else lb[k]
            nxt[side] = t + 500
            j = vl.at(t + 500)
            if not w.lt.valid(j):
                continue
            p, s = (w.lt.ap[j, 0], w.lt.as_[j, 0]) if a else (w.lt.bp[j, 0], w.lt.bs[j, 0])
            if not (s >= MIN_SZ and (p <= lpx + 1e-9 if a else p >= lpx - 1e-9)):
                continue
            nxt[side] = t + 500 + 5000
            h = vp.at(t + 1000)
            if not w.pm.valid(h):
                continue
            hp, hs = ((1 - w.pm.bp[h, 0]), w.pm.bs[h, 0]) if a else (w.pm.ap[h, 0], w.pm.as_[h, 0])
            qty = min(s, hs, 200.0)
            if qty < MIN_SZ:
                continue
            pnl = (1 - p - hp if a else p - hp) - fl(p) - w.rate * hp * (1 - hp)
            out.append(dict(e=w.e, t=t, side=side, qty=qty, pnl_sh=pnl, usd=pnl * qty, late=(w.end - t) <= 120000))
    return out
