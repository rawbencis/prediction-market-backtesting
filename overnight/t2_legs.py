"""T2: two legs that never communicate. Leg P = Polymarket BTC/ETH 15m (as from Dublin), leg L = Lighter 15m (Tokyo).

Each leg sees only its own venue (book + prints), the window clock and fixed parameters. A window's result is both legs
settled together (P on Polymarket's winner, L on Lighter's outcome). Because the legs never communicate, the window
P&L is exactly P&L(P) + P&L(L): pairing cannot create edge, it can only change variance and unhedged exposure.

Clocks: Polymarket in exchange time (ms) with the `live` scenario (seen +9 ms; post +55 ms calm, +190 ms if the best
prices moved in the last 1 s; cancel +90 ms; taker hold 150 ms; strict maker fills: only prints through our price, each
filling at most its own size). Lighter in its own exchange time (ms): seen +3 ms, post 0 ms, taker hold 300 ms (alt 140),
cancel 300 ms (alt 0); maker fills only from recorded Lighter taker trades strictly through our price.
Every position is "long token X (UP or DOWN) at cost c"; settlement value is W (UP) or 1 - W (DOWN) on that venue.

  day --day D        per-window, per-cell leg results -> out/t2/<day>.pkl
  agg --days D1 D2   cell table (dev or holdout), placebo, guess accuracy
"""

import argparse
import csv
import json
import pickle
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/home/ubuntu/lighter-shadow")
import lighter_eval as le  # noqa: E402
import t1_arb  # noqa: E402

OUT = Path(__file__).resolve().parent / "out" / "t2"
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("ud", "u1"), ("side", "u1"),
                  ("p", "<u2"), ("s", "<f4"), ("fee", "<u2"), ("tx", "S32")])
QS = (5.0, 50.0)
CLOSE_MS = 900_000
P_SEEN, P_POST_CALM, P_POST_MOVED, P_CANCEL, P_HOLD = 9, 55, 190, 90, 150
L_SEEN, L_POST = 3, 0
L_HOLDS, L_CANCELS = (300, 140), (300, 0)
END_REST = CLOSE_MS - 30_000


def frozen_grid():
    g = {"A": [], "B": [], "C": [], "D": []}
    for s in (0.99, 0.98, 0.96, 0.94):
        for anchor in ("sym", "mid"):
            for post in (5, 60, 300):
                for cancel in (None, 450):
                    if cancel is not None and cancel <= post + 30:
                        continue
                    g["A"].append(dict(s=s, anchor=anchor, post=post, cancel=cancel))
    for x in (0.02, 0.04):
        for w in (1, 5):
            for mode in ("Pfollow_Lfade", "Pfade_Lfollow"):
                for entry in ("taker", "maker"):
                    g["B"].append(dict(x=x, w=w, mode=mode, entry=entry))
    for st in (1000, 3000):
        for x in (0.02, 0.04):
            for orient in ("L_up_P_down", "L_down_P_up"):
                for pentry in ("taker", "join"):
                    g["C"].append(dict(stale_ms=st, x=x, orient=orient, pentry=pentry))
    for s in (0.98, 0.96):
        for post in (5, 60):
            g["D"].append(dict(s=s, anchor="sym", post=post, deadline=CLOSE_MS - 60_000))
    return g


# ---------------------------------------------------------------- venue views (one window, times in ms from open)
class Venue:
    """Top of book of the UP token + prints. Long DOWN = short UP at 1 - c."""

    def __init__(self, t, b, k, bs, ks, pt, pp, ps, seen):
        self.t, self.b, self.k, self.bs, self.ks = t, b, k, bs, ks
        self.pt, self.pp, self.ps = pt, pp, ps  # prints, as UP-equivalent price
        self.seen = seen
        ok = (b > 0) & (k > b)
        self.mid = np.where(ok, (b + k) / 2, np.nan)
        self.ok = ok
        chg = np.ones(len(t), bool)
        chg[1:] = (b[1:] != b[:-1]) | (k[1:] != k[:-1])
        self.tchg = t[chg]

    def at(self, tq):
        """index of the book row visible at exchange time tq (as-of, no lookahead)."""
        return np.searchsorted(self.t, tq, "right") - 1

    def seen_at(self, tq):
        return self.at(tq - self.seen)

    def ask(self, i, tok):  # price, size to buy token
        if i < 0 or not self.ok[i]:
            return np.nan, 0.0
        return (self.k[i], self.ks[i]) if tok == "UP" else (1 - self.b[i], self.bs[i])

    def bid(self, i, tok):  # price, size to sell token
        if i < 0 or not self.ok[i]:
            return np.nan, 0.0
        return (self.b[i], self.bs[i]) if tok == "UP" else (1 - self.k[i], self.ks[i])

    def midtok(self, tq, tok):
        i = self.at(tq)
        if i < 0:
            return np.nan
        m = self.mid[i]
        return m if tok == "UP" else 1 - m

    def moved_within(self, tq, span=1000):
        j = np.searchsorted(self.tchg, tq, "right")
        return j > 0 and self.tchg[j - 1] > tq - span

    def maker_fill(self, tok, price, t0, t1, q):
        """Resting bid for tok at price, live [t0, t1]. Strict: UP-equivalent prints strictly through (UP bid at c fills on
        UP prints < c; DOWN bid at c = UP ask at 1 - c fills on UP prints > 1 - c). -> (qty, first fill time)."""
        m = (self.pt >= t0) & (self.pt <= t1)
        m &= (self.pp < price - 1e-9) if tok == "UP" else (self.pp > 1 - price + 1e-9)
        if not m.any():
            return 0.0, np.nan
        sz = np.cumsum(self.ps[m])
        return float(min(q, sz[-1])), float(self.pt[m][0])


def venue_pm(P, T, e):
    t = P["x"].astype(np.int64) - e * 1000
    o = np.argsort(t, kind="stable")
    P, t = P[o], t[o]
    pt = T["x"].astype(np.int64) - e * 1000
    pp = np.where(T["ud"] == 0, T["p"] / 1e4, 1 - T["p"] / 1e4)
    o2 = np.argsort(pt, kind="stable")
    return Venue(t, P["b"] / 1e4, P["k"] / 1e4, P["bs"].astype(float), P["ks"].astype(float), pt[o2], pp[o2],
                 T["s"][o2].astype(float), P_SEEN)


def venue_l(L, T, e):
    t = L["x"].astype(np.int64) - e * 1000
    o = np.argsort(t, kind="stable")
    L, t = L[o], t[o]
    pt = np.where(T["T"] > 0, T["T"], T["l"] // 1000).astype(np.int64) - e * 1000
    o2 = np.argsort(pt, kind="stable")
    return Venue(t, L["b1"] / 1e4, L["k1"] / 1e4, L["b1s"].astype(float), L["k1s"].astype(float), pt[o2],
                 (T["p"] / 1e4)[o2], T["s"][o2].astype(float), L_SEEN)


# ---------------------------------------------------------------- leg actions
def leg_result(tok, q, cost, tfill, V, W, fee_c=0.0, exit_px=None):
    """One leg's outcome. pnl to settlement (or to exit price if exited), markouts at 1/5/30 s vs own mid."""
    if q <= 0:
        return dict(tok=tok, q=0.0, cost=np.nan, pnl=0.0, mo1=np.nan, mo5=np.nan, mo30=np.nan, t=np.nan)
    val = (W if tok == "UP" else 1 - W) if exit_px is None else exit_px
    fee = fee_c * cost * (1 - cost)
    mo = [V.midtok(tfill + d, tok) - cost - fee for d in (1000, 5000, 30000)]
    return dict(tok=tok, q=q, cost=cost, pnl=q * (val - cost - fee), mo1=mo[0], mo5=mo[1], mo30=mo[2], t=tfill)


def p_taker(V, tok, tdec, q, fee_c):
    """Polymarket taker: decide at tdec (book seen), limit = ask seen; arrives +55/190 ms, held 150 ms, then matches."""
    i0 = V.seen_at(tdec)
    lim, _ = V.ask(i0, tok)
    if lim != lim:
        return None
    post = P_POST_MOVED if V.moved_within(tdec - P_SEEN) else P_POST_CALM
    tm = tdec + post + P_HOLD
    px, sz = V.ask(V.at(tm), tok)
    fill = px == px and px <= lim + 1e-9 and sz >= 5
    px2, sz2 = V.ask(V.at(tm + 100), tok)
    pess = fill and px2 == px2 and px2 <= lim + 1e-9 and sz2 >= 5
    return dict(fill=fill, pess=pess, px=float(px) if fill else np.nan, qty=float(min(q, sz)) if fill else 0.0, tm=tm, lim=lim)


def l_taker(V, tok, tdec, q, hold):
    i0 = V.seen_at(tdec)
    lim, _ = V.ask(i0, tok)
    if lim != lim:
        return None
    tm = tdec + L_POST + hold
    px, sz = V.ask(V.at(tm), tok)
    fill = px == px and px <= lim + 1e-9 and sz >= 5
    px2, sz2 = V.ask(V.at(tm + 100), tok)
    pess = fill and px2 == px2 and px2 <= lim + 1e-9 and sz2 >= 5
    return dict(fill=fill, pess=pess, px=float(px) if fill else np.nan, qty=float(min(q, sz)) if fill else 0.0, tm=tm, lim=lim)


def rest(V, venue, tok, price, tdec, tend_dec, q):
    """Post-only bid for tok at price from decision tdec to cancel decision tend_dec. -> (qty, tfill, live_from) or None if
    it would cross (post-only reject)."""
    i0 = V.seen_at(tdec)
    ap, _ = V.ask(i0, tok)
    if ap == ap and price >= ap - 1e-9:
        return None
    if venue == "P":
        live0 = tdec + (P_POST_MOVED if V.moved_within(tdec - P_SEEN) else P_POST_CALM)
        live1 = tend_dec + P_CANCEL
    else:
        live0, live1 = tdec + L_POST, tend_dec + L_CANCELS[0]
    qty, tf = V.maker_fill(tok, price, live0, live1, q)
    return qty, tf, live0


def ctick(x, up):
    return (np.ceil(x * 100 - 1e-9) if up else np.floor(x * 100 + 1e-9)) / 100


# ---------------------------------------------------------------- families
def fam_A(c, VP, VL, WP, WL, fee_p, q):
    s, tpost = c["s"], c["post"] * 1000
    tend = (c["cancel"] * 1000) if c["cancel"] else END_REST
    if c["anchor"] == "sym":
        a = ctick(s / 2, False)
        b = round(s - a, 2)
    else:
        mp = VP.mid[VP.seen_at(tpost)] if VP.seen_at(tpost) >= 0 else np.nan
        ml = VL.mid[VL.seen_at(tpost)] if VL.seen_at(tpost) >= 0 else np.nan
        if mp != mp or ml != ml:
            return None
        a = ctick(mp - (1 - s) / 2, False)
        b = ctick((1 - ml) - (1 - s) / 2, False)
    if not (0.01 <= a <= 0.99 and 0.01 <= b <= 0.99):
        return None
    rp = rest(VP, "P", "UP", a, tpost, tend, q)
    rl = rest(VL, "L", "DOWN", b, tpost, tend, q)
    P = leg_result("UP", rp[0], a, rp[1], VP, WP) if rp else leg_result("UP", 0, 0, 0, VP, WP)
    L = leg_result("DOWN", rl[0], b, rl[1], VL, WL) if rl else leg_result("DOWN", 0, 0, 0, VL, WL)
    P["posted"], L["posted"] = rp is not None, rl is not None
    P["price"], L["price"] = a, b
    return P, L


def mid_move_trigger(V, x, w, t0=None, t1=END_REST):
    """first time t (on own-venue book events, as seen) where |mid(t) - mid(t - w)| >= x, spread <= 10c at both ends."""
    t0 = w * 1000 if t0 is None else t0
    tt = V.t + V.seen
    good = V.ok & ((V.k - V.b) <= 0.1001)
    m = np.where(good, V.mid, np.nan)
    sel = np.flatnonzero((tt >= t0) & (tt <= t1) & good)
    if not len(sel):
        return None
    j = np.searchsorted(V.t, V.t[sel] - w * 1000, "right") - 1
    ok = j >= 0
    sel, j = sel[ok], j[ok]
    d = m[sel] - m[j]
    hit = np.flatnonzero(np.abs(d) >= x - 1e-9)
    if not len(hit):
        return None
    k = hit[0]
    return float(tt[sel[k]]), float(np.sign(d[k]))


def fam_B(c, VP, VL, WP, WL, fee_p, q):
    out = []
    for leg, V, W in (("P", VP, WP), ("L", VL, WL)):
        tr = mid_move_trigger(V, c["x"], c["w"])
        follow = (c["mode"] == "Pfollow_Lfade") == (leg == "P")
        if tr is None:
            out.append(dict(leg_result("UP", 0, 0, 0, V, W), trig=False))
            continue
        t, sgn = tr
        tok = ("UP" if sgn > 0 else "DOWN") if follow else ("DOWN" if sgn > 0 else "UP")
        out.append(entry(c["entry"], leg, V, W, tok, t, fee_p, q))
    return out[0], out[1]


def entry(kind, leg, V, W, tok, t, fee_p, q):
    if kind == "taker":
        f = p_taker(V, tok, t, q, fee_p) if leg == "P" else l_taker(V, tok, t, q, L_HOLDS[0])
        if not f or not f["fill"]:
            return dict(leg_result(tok, 0, 0, 0, V, W), trig=True, pess=False)
        r = leg_result(tok, f["qty"], f["px"], f["tm"], V, W, fee_p if leg == "P" else 0.01)
        r.update(trig=True, pess=f["pess"])
        if leg == "L":  # alt Lighter hold 140 ms
            f2 = l_taker(V, tok, t, q, L_HOLDS[1])
            r["fill140"] = bool(f2 and f2["fill"])
        return r
    i0 = V.seen_at(t)
    bp, _ = V.bid(i0, tok)
    if bp != bp:
        return dict(leg_result(tok, 0, 0, 0, V, W), trig=True)
    rr = rest(V, leg, tok, float(bp), t, END_REST, q)
    if not rr:
        return dict(leg_result(tok, 0, 0, 0, V, W), trig=True)
    r = leg_result(tok, rr[0], float(bp), rr[1], V, W)
    r["trig"] = True
    return r


def stale_trigger(V, st, x, quote):
    """quote 'ask' stale: ask unchanged >= st ms while bid rose >= x over that span -> take the ask (long UP).
    quote 'bid' stale: bid unchanged >= st ms while ask fell >= x -> hit the bid (long DOWN). First such event, as seen."""
    tt = V.t + V.seen
    q_ = V.k if quote == "ask" else V.b
    o_ = V.b if quote == "ask" else V.k
    chg = np.ones(len(V.t), bool)
    chg[1:] = q_[1:] != q_[:-1]
    last_chg = V.t[np.maximum.accumulate(np.where(chg, np.arange(len(V.t)), 0))]
    cand = np.flatnonzero(V.ok & (tt >= 5000) & (tt <= END_REST) & (V.t - last_chg >= st))
    if not len(cand):
        return None
    j = np.searchsorted(V.t, last_chg[cand], "right") - 1
    jj = np.maximum(j, 0)
    mv = (o_[cand] - o_[jj]) if quote == "ask" else (o_[jj] - o_[cand])
    hit = np.flatnonzero((j >= 0) & V.ok[jj] & (mv >= x - 1e-9))
    if len(hit):
        return float(tt[cand[hit[0]]])
    return None


def fam_C(c, VP, VL, WP, WL, fee_p, q):
    if c["orient"] == "L_up_P_down":
        lq, ltok, pq, ptok = "ask", "UP", "bid", "DOWN"
    else:
        lq, ltok, pq, ptok = "bid", "DOWN", "ask", "UP"
    tl = stale_trigger(VL, c["stale_ms"], c["x"], lq)
    L = entry("taker", "L", VL, WL, ltok, tl, fee_p, q) if tl is not None else dict(leg_result(ltok, 0, 0, 0, VL, WL), trig=False)
    tp = stale_trigger(VP, c["stale_ms"], c["x"], pq)
    P = (entry("taker" if c["pentry"] == "taker" else "maker", "P", VP, WP, ptok, tp, fee_p, q) if tp is not None
         else dict(leg_result(ptok, 0, 0, 0, VP, WP), trig=False))
    return P, L


def fam_D(c, VP, VL, WP, WL, fee_p, q):
    """Static symmetric pair with partner guessing. P bids UP at a; L bids DOWN at b (= sells UP at 1 - b).
    Each leg infers 'partner filled' when its own venue printed through the partner's price after the partner went live."""
    s, tpost, dl = c["s"], c["post"] * 1000, c["deadline"]
    a = ctick(s / 2, False)
    b = round(s - a, 2)
    res, truth = {}, {}
    # actual resting outcomes (no reaction) give the truth for each leg's guess
    rp = rest(VP, "P", "UP", a, tpost, END_REST, q)
    rl = rest(VL, "L", "DOWN", b, tpost, END_REST, q)
    if rp is None or rl is None:
        return None
    for leg, V, W, tok, price, own, other, ptok, pprice in (
        ("P", VP, WP, "UP", a, rp, rl, "DOWN", b),
        ("L", VL, WL, "DOWN", b, rl, rp, "UP", a),
    ):
        # inferred partner fill time: own venue prints through partner's price (partner = bid on ptok at pprice)
        live0 = own[2]
        m = (V.pt >= live0) & (V.pt <= END_REST)
        m &= (V.pp < pprice - 1e-9) if ptok == "UP" else (V.pp > 1 - pprice + 1e-9)
        t_inf = float(V.pt[m][0]) if m.any() else np.nan
        own_q, own_t = own[0], own[1]
        actual_partner_t = other[1] if other[0] > 0 else np.nan
        if t_inf == t_inf and (own_q <= 0 or own_t > t_inf):
            # partner (inferred) filled first and I have not: cancel my bid and cross to complete
            tdec = t_inf + V.seen
            f = p_taker(V, tok, tdec, q, fee_p) if leg == "P" else l_taker(V, tok, tdec, q, L_HOLDS[0])
            qf = f["qty"] if f and f["fill"] else 0.0
            r = leg_result(tok, qf, f["px"] if qf else 0, f["tm"] if qf else 0, V, W, fee_p if leg == "P" else 0.01)
            act = "cross"
            guess_t = tdec
            guess_partner = True
        elif own_q > 0 and not (t_inf == t_inf and t_inf <= dl):
            # I filled, partner not inferred filled by the deadline: exit at my venue's bid as taker
            r = leg_result(tok, own_q, price, own_t, V, W)
            i = V.at(dl + (P_POST_CALM + P_HOLD if leg == "P" else L_HOLDS[0]))
            bp, bs = V.bid(i, tok)
            if bp == bp and bs >= 5:
                fee = (fee_p if leg == "P" else 0.01) * bp * (1 - bp)
                r["pnl"] = own_q * (bp - fee - price)
                act = "exit"
            else:
                act = "exit_failed"
            guess_t, guess_partner = dl, False
        else:
            r = leg_result(tok, own_q, price, own_t, V, W)
            act = "hold"
            guess_t, guess_partner = END_REST, bool(t_inf == t_inf)
        actual = bool(actual_partner_t == actual_partner_t and actual_partner_t <= guess_t)
        r.update(act=act, guess=guess_partner, actual=actual)
        res[leg] = r
    return res["P"], res["L"]


FAMS = {"A": fam_A, "B": fam_B, "C": fam_C, "D": fam_D}


def cid(fam, c):
    return fam + ":" + ",".join(f"{k}={v}" for k, v in c.items())


def cmd_day(a):
    d = date.fromisoformat(a.day)
    t0 = t1_arb.utc(d)
    t1 = t0 + 86400
    keep = lambda x: (x["tf"] == 15) & (x["a"] <= 1) & (x["e"] >= t0) & (x["e"] < t1)  # noqa: E731
    LB = t1_arb.load_hours(t1_arb.LT_DIR, "lb", le.LB_DT, d, keep)
    LT = t1_arb.load_hours(t1_arb.LT_DIR, "lt", le.LT_DT, d, keep)
    _, first = np.unique(LT["id"], return_index=True)
    LT = LT[np.sort(first)]
    PM = t1_arb.load_hours(t1_arb.PM_DIR, "pm", le.PM_DT, d, keep)
    PT = t1_arb.load_hours(t1_arb.PM_DIR, "tr", TR_DT, d, keep)
    meta = t1_arb.read_meta()
    with (t1_arb.LT_DIR / "lighter_markets.csv").open() as f:
        mk = {int(r[3]): (r[0], int(r[2])) for r in csv.reader(f) if len(r) >= 4}
    with (t1_arb.LT_DIR / "lighter_outcomes.csv").open() as f:
        lo = {mk[int(r[0])]: r[2] for r in csv.reader(f) if len(r) >= 3 and int(r[0]) in mk}
    grid = frozen_grid()
    keys = sorted({(int(x), int(y)) for x, y in zip(LB["a"], LB["e"])} & {(int(x), int(y)) for x, y in zip(PM["a"], PM["e"])})
    res = []
    for ai, e in keys:
        nm = le.lr.ASSETS[ai]
        WP, fee_p, _ = meta.get((nm, e), (np.nan, np.nan, ""))
        if WP != WP:
            continue
        code = lo.get((nm, e))
        WL = {"2": 1.0, "1": 0.0}.get(code, np.nan)  # decoded against PM winners in T1 (2 = Up, 1 = Down)
        if WL != WL:
            continue
        P = PM[(PM["a"] == ai) & (PM["e"] == e)]
        T = PT[(PT["a"] == ai) & (PT["e"] == e)]
        L = LB[(LB["a"] == ai) & (LB["e"] == e)]
        LT_ = LT[(LT["a"] == ai) & (LT["e"] == e)]
        if len(P) < 10 or len(L) < 10:
            continue
        VP, VL = venue_pm(P, T, e), venue_l(L, LT_, e)
        row = dict(asset=nm, e=e, WP=WP, WL=WL, fee=fee_p, has_ptr=len(T) > 0, cells={})
        for fam, cells in grid.items():
            for c in cells:
                for q in QS:
                    r = FAMS[fam](c, VP, VL, WP, WL, fee_p, q)
                    if r is not None:
                        row["cells"][(cid(fam, c), q)] = r
        res.append(row)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"{d}.pkl").open("wb") as f:
        pickle.dump(res, f)
    print(f"{d}: {len(res)} windows (PM trades present in {sum(r['has_ptr'] for r in res)})", flush=True)


# ---------------------------------------------------------------- aggregation
def boot(x, B=2000, seed=1):
    x = np.asarray(x, float)
    if len(x) < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    m = x[rng.integers(0, len(x), (B, len(x)))].mean(1)
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def summarize(rows, key, rng=None, placebo=False):
    """rows: windows having this cell. Returns a dict of cell stats. placebo: L legs re-paired across windows at random."""
    Ps = [w["cells"][key][0] for w in rows]
    Ls = [w["cells"][key][1] for w in rows]
    if placebo:
        perm = rng.permutation(len(Ls))
        # avoid self-pairing
        same = perm == np.arange(len(Ls))
        perm[same] = np.roll(perm, 1)[same]
        Ls = [Ls[i] for i in perm]
    n = len(rows)
    qp = np.array([p["q"] for p in Ps])
    ql = np.array([x["q"] for x in Ls])
    pnl = np.array([p["pnl"] + x["pnl"] for p, x in zip(Ps, Ls)])
    both, one = (qp > 0) & (ql > 0), (qp > 0) ^ (ql > 0)
    sh = (qp + ql) / 2
    cost = np.array([p["cost"] + x["cost"] for p, x in zip(Ps, Ls)])
    tot_sh = sh.sum()
    per_w = pnl  # per window $
    days = len({w["e"] // 86400 for w in rows}) or 1
    cps = 100 * pnl.sum() / tot_sh if tot_sh else np.nan
    # 90% interval of cents/share by window resampling (ratio estimator)
    if n >= 5 and tot_sh > 0:
        r_ = np.random.default_rng(2)
        idx = r_.integers(0, n, (2000, n))
        s_ = sh[idx].sum(1)
        b_ = 100 * pnl[idx].sum(1) / np.where(s_ > 0, s_, np.nan)
        lo_, hi_ = np.nanpercentile(b_, 5), np.nanpercentile(b_, 95)
        se = np.nanstd(b_)
    else:
        lo_ = hi_ = se = np.nan
    mo = lambda L_, k: np.nanmean([x[k] for x in L_ if x["q"] > 0]) * 100 if any(x["q"] > 0 for x in L_) else np.nan  # noqa: E731
    return dict(n=n, both=int(both.sum()), one=int(one.sum()), none=int((~both & ~one).sum()),
                pair_cost=float(np.nanmean(cost[both])) if both.any() else np.nan,
                pnl_paired=float(pnl[both].mean()) if both.any() else np.nan,
                pnl_one=float(pnl[one].mean()) if one.any() else np.nan,
                unhedged=float(np.abs(qp - ql).mean()), cps=cps, lo=lo_, hi=hi_, se=se,
                usd_day=float(pnl.sum() / days), sd_window=float(per_w.std()),
                fills_day=float(((qp > 0).sum() + (ql > 0).sum()) / days),
                P_cps=100 * sum(p["pnl"] for p in Ps) / max(qp.sum(), 1e-9), L_cps=100 * sum(x["pnl"] for x in Ls) / max(ql.sum(), 1e-9),
                P_usd_day=sum(p["pnl"] for p in Ps) / days, L_usd_day=sum(x["pnl"] for x in Ls) / days,
                P_mo1=mo(Ps, "mo1"), P_mo5=mo(Ps, "mo5"), L_mo1=mo(Ls, "mo1"), L_mo5=mo(Ls, "mo5"),
                P_fill=float((qp > 0).mean()), L_fill=float((ql > 0).mean()), days=days)


def cmd_agg(a):
    rows = []
    for p in sorted(OUT.glob("*.pkl")):
        if a.days[0] <= p.stem <= a.days[1]:
            rows += pickle.load(p.open("rb"))
    keys = sorted({k for w in rows for k in w["cells"]})
    rng = np.random.default_rng(7)
    out = []
    for asset in ("btc", "eth", "all"):
        R = rows if asset == "all" else [w for w in rows if w["asset"] == asset]
        for k in keys:
            rr = [w for w in R if k in w["cells"]]
            if len(rr) < 5:
                continue
            s = summarize(rr, k)
            pl = [summarize(rr, k, rng, placebo=True) for _ in range(20)]
            s.update(asset=asset, cell=k[0], q=k[1], plc_both=np.mean([x["both"] for x in pl]),
                     plc_sd=np.mean([x["sd_window"] for x in pl]), plc_unhedged=np.mean([x["unhedged"] for x in pl]),
                     plc_pnl_paired=np.nanmean([x["pnl_paired"] for x in pl]))
            if k[0].startswith("D:"):
                for leg_i, leg in ((0, "P"), (1, "L")):
                    g = [w["cells"][k][leg_i] for w in rr]
                    acts = defaultdict(list)
                    for x in g:
                        acts[(x["act"], x["guess"] == x["actual"])].append(x["pnl"])
                    s[f"{leg}_guess_wrong"] = float(np.mean([x["guess"] != x["actual"] for x in g]))
                    s[f"{leg}_acts"] = {f"{k2[0]}|{'right' if k2[1] else 'wrong'}": (len(v), round(float(np.mean(v)), 3)) for k2, v in acts.items()}
            if k[0].startswith(("B:", "C:")):
                for leg_i, leg in ((0, "P"), (1, "L")):
                    g = [w["cells"][k][leg_i] for w in rr]
                    tg = [x for x in g if x.get("trig")]
                    s[f"{leg}_trig"] = len(tg)
                    if "pess" in (tg[0] if tg else {}):
                        s[f"{leg}_pess_fill"] = float(np.mean([bool(x.get("pess")) for x in tg]))
                    if leg == "L" and tg and "fill140" in tg[0]:
                        s["L_fill140"] = float(np.mean([bool(x.get("fill140")) for x in tg]))
            out.append(s)
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"{a.days[0]}_{a.days[1]}"
    with (OUT / f"agg_{tag}.pkl").open("wb") as f:
        pickle.dump(out, f)
    print(f"{len(rows)} windows, {len(keys)} cell keys, {len(out)} rows -> agg_{tag}.pkl")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("day")
    p.add_argument("--day", required=True)
    p = sp.add_parser("agg")
    p.add_argument("--days", nargs=2, required=True)
    p = sp.add_parser("grid")
    a = ap.parse_args()
    if a.cmd == "grid":
        print(json.dumps(frozen_grid(), indent=1))
        return
    {"day": cmd_day, "agg": cmd_agg}[a.cmd](a)


if __name__ == "__main__":
    main()
