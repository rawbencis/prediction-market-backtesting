"""F9 grid under the 'live' scenario (f9_live): post-only BUY 5 makers on BTC 5m (or 15m), pess queue, hold to settlement.
Cancels: signal seen + book (+9) + cancel (+90), never before order-on-book + cancel (ack). Book changes are seen +9.
Alt events: an ETH/SOL/XRP/DOGE 5m UP mid moves >= THR (1e-4 $) vs 500 ms earlier (f7_altpull.alt_events, per alt).

Items (config ids):
  2  alt|THR|NEED|TTL|SKIP   NEED alts moved the same way within 500 ms -> BUY the favoured side (UP if alts rose) one
                             tick inside a >= 2-tick spread, else at the best bid. Pulls: an alt move the other way (THR),
                             someone improving past us, TTL. SKIP=y: not if BTC's own best moved that way in the last 300 ms.
  3  hedge|CAP|<item-2 id>   item-2 config + after each fill a resting bid on the other token at pair cost <= CAP (and one
                             tick below its ask), until 10 s before the close, never cancelled otherwise.
  4  pair|ALT                BTC best bid/ask unchanged 1 s -> bid both tokens at their best bids at once. ALT=y: cancel both
                             on any alt move (THR 50) unless a fill was seen first; after one leg fills the other rests to
                             20 s before the close. Next pair after both orders are done.
  5  pre|LEVEL|CXL           earliest pre-open book row: BUY 5 both tokens at LEVEL; CXL=alt: cancel on an alt move against
                             that token (THR 50), never: rest to the close.
  6  calm|IMB|OFF|TTL        BTC best unchanged 1 s -> join the best bid (+OFF ticks) of the token whose top-of-book
                             imbalance > IMB (UP: (bid size - ask size)/(sum)); cancel on an alt move against (THR 50); TTL
                             30 s or 'close' (20 s before the close). One live order per token.
  8  imb|IMB|TTL|ALT         a token's top-of-book imbalance > IMB and its mid up >= 0.25c over 1 s -> BUY 5 at its best bid;
                             rest until imbalance < 0, TTL (30 s / 120 s) or 10 s before the close; ALT=y: alt pull too.
  7  = item 2 with BTC 15m as the target (run with TF=15; alt events from the 5m alt windows inside the 15m window).
Entries (items 2, 4, 6) only from the window start to 20 s before the close.
Usage: PMLAT_MAXDAY=2026-11-16 python f9_grid.py DAY SCEN TF [cfg;cfg|all] -> out/pmlat/f9-{scen}-{tf}m-{day}{tag}.csv.gz
       (+ -orders.csv, -pairs.csv)
"""
import csv
import gzip
import itertools
import os
import sys

import numpy as np

import f9_live  # noqa: F401
import pmlat
import dsim
import f3_maker
from f7_altpull import ALTS
from f8_grid import next_stable

LOOK = 500
SIZE = 5.0

ITEM2 = ["alt|" + "|".join(c) for c in itertools.product(("50", "100"), ("1", "2"), ("5000", "30000"), ("y", "n"))]
ITEM4 = ["pair|y", "pair|n"]
ITEM5 = ["pre|" + "|".join(c) for c in itertools.product(("4900", "4800", "4700"), ("alt", "never"))]
ITEM6 = ["calm|" + "|".join(c) for c in itertools.product(("0.3", "0.6"), ("0", "-1"), ("30000", "close"))]
ITEM8 = ["imb|" + "|".join(c) for c in itertools.product(("0.3", "0.6"), ("30000", "120000"), ("y", "n"))]
ALL5 = ITEM2 + ITEM4 + ITEM5 + ITEM6  # item 8 runs as its own pass (F9_TAG=-i8, cfgs ITEM8)
ALL15 = ITEM2


def alt_per_alt(alt_mk, thr):
    """-> {alt index: (up times, down times)} for alts with a live book."""
    out = {}
    for n, m in enumerate(alt_mk):
        B = m["book"]
        ok = (B["b"] > 0) & (B["k"] > 0)
        x = B["x"][ok].astype(np.int64)
        if len(x) < 2:
            continue
        mid = (B["b"][ok].astype(np.int64) + B["k"][ok].astype(np.int64)) / 2
        j = np.searchsorted(x, x - LOOK, "right") - 1
        dm = mid - np.where(j >= 0, mid[np.clip(j, 0, None)], np.nan)
        out[n] = (x[dm >= thr], x[dm <= -thr])
    return out


def triggers(pa, need, d):
    """Times t (sorted) at which >= need distinct alts had an event of direction d (0 up, 1 down) in (t - 500, t]."""
    ev = sorted((int(t), a) for a, v in pa.items() for t in v[d])
    if need <= 1:
        return np.array([t for t, _ in ev], np.int64)
    out, lo = [], 0
    for i, (t, a) in enumerate(ev):
        while ev[lo][0] <= t - LOOK:
            lo += 1
        if len({aa for _, aa in ev[lo:i + 1]}) >= need:
            out.append(t)
    return np.array(out, np.int64)


def union(pa, d):
    z = [v[d] for v in pa.values()]
    return np.sort(np.concatenate(z)) if z else np.zeros(0, np.int64)


class Mkt:
    def __init__(self, m, winner, alt_mk, sc):
        self.m, self.sc, self.winner = m, sc, winner
        B = m["book"]
        self.x = B["x"].astype(np.int64)
        self.b, self.k = B["b"].astype(np.int64), B["k"].astype(np.int64)
        self.bs, self.ks = B["bs"].astype(float), B["ks"].astype(float)
        self.e, tf = m["key"][2], m["key"][1]
        self.end = (self.e + tf * 60) * 1000
        ch = np.r_[True, (self.b[1:] != self.b[:-1]) | (self.k[1:] != self.k[:-1])]
        self.C = self.x[ch]
        self.alt = alt_mk
        self._pa = {}

    def pa(self, thr):
        if thr not in self._pa:
            self._pa[thr] = alt_per_alt(self.alt, thr)
        return self._pa[thr]

    def seen(self, t):
        """Index of the book row seen at decision time t (exchange rows up to t - book)."""
        return int(np.searchsorted(self.x, t - self.sc["book"], "right")) - 1

    def post(self, tok, price, t_dec, t_end, pulls, against=None):
        """One post-only BUY. pulls: subset of {'out'}; against: sorted alt-event times that cancel it.
        -> dict(t_arr, t_cxl, status, fills) or None if not sent."""
        sc, x, b, k = self.sc, self.x, self.b, self.k
        if not 100 <= price <= 9900:
            return None
        t_arr = t_dec + pmlat.olat(self.m, t_dec, sc)
        if t_end <= t_arr:
            return None
        t_cxl = t_end + sc["cancel"]
        i0 = self.seen(t_dec) + 1
        i1 = int(np.searchsorted(x, t_end - sc["book"], "left"))
        if i1 > i0:
            bb, kk = b[i0:i1], k[i0:i1]
            trig = (bb == 0) & (kk == 0)
            if "out" in pulls:
                trig |= (bb > price) if tok == 0 else ((kk < 10000 - price) & (kk > 0))
            j = np.flatnonzero(trig)
            if len(j):
                t_cxl = min(t_cxl, int(x[i0 + j[0]]) + sc["book"] + sc["cancel"])
        if against is not None and len(against):
            q = int(np.searchsorted(against, t_dec - sc["book"], "right"))  # alt rows not yet seen at decision
            if q < len(against):
                t_cxl = min(t_cxl, int(against[q]) + sc["book"] + sc["cancel"])
        t_cxl = max(t_cxl, t_arr + sc["cancel"])
        res = f3_maker.MAKER(self.m, t_arr, tok, price, SIZE, t_cxl, model="pess")
        return dict(t_arr=t_arr, t_cxl=t_cxl, status=res["status"], fills=res["fills"], tok=tok, price=price, t_dec=t_dec)

    def rec(self, cfg, o, role, **kw):
        f = o["fills"]
        tf = int(f[0]["x"])
        tok, px = o["tok"], o["price"]
        r = dict(cfg=cfg, e=self.e, role=role, tok=tok, px=px, filled=float(sum(z["size"] for z in f)), tfill=tf,
                 tleft=self.end - tf, olat=o["t_arr"] - o["t_dec"], t_live=o["t_cxl"] - o["t_arr"])
        for h in (1000, 5000):
            mm = dsim.mid(self.m, tf + h, tok) if tf + h < self.end else None
            r[f"mo{h}"] = None if mm is None else (mm - px) / 100
        if self.winner is not None:
            r["settle_c"] = 100 * ((1.0 if (self.winner == "UP") == (tok == 0) else 0.0) - px / 1e4)
        r.update(kw)
        return r


def best_bid(M, i, tok):
    return int(M.b[i]) if tok == 0 else (10000 - int(M.k[i]) if M.k[i] else 0)


def run_alt(M, cfg, out, cnt, hedge_cap=None, label=None):
    _, thr, need, ttl, skip = cfg.split("|")
    thr, need, ttl = int(thr), int(need), int(ttl)
    label = label or cfg
    pa = M.pa(thr)
    up_any, dn_any = union(pa, 0), union(pa, 1)
    busy = [-1, -1]
    t0, t1 = M.e * 1000, M.end - 20000
    for d in (0, 1):  # d = 0: alts rose -> BUY UP (tok 0); d = 1: alts fell -> BUY DOWN (tok 1)
        tok = d
        against = dn_any if d == 0 else up_any
        for ta in triggers(pa, need, d):
            t_dec = int(ta) + M.sc["book"]
            if not t0 <= t_dec <= t1 or t_dec < busy[tok]:
                continue
            i = M.seen(t_dec)
            if i < 0 or M.b[i] == 0 or M.k[i] == 0:
                continue
            if skip == "y":  # BTC's own best already moved that way within the last 300 ms (as seen)
                j0 = int(np.searchsorted(M.x, t_dec - M.sc["book"] - 300, "right"))
                if j0 <= i and j0 >= 1:
                    db, dk = np.diff(M.b[j0 - 1:i + 1]), np.diff(M.k[j0 - 1:i + 1])
                    moved = ((db > 0) | (dk > 0)).any() if d == 0 else ((db < 0) | (dk < 0)).any()
                    if moved:
                        continue
            spr = int(M.k[i]) - int(M.b[i])
            price = best_bid(M, i, tok) + (100 if spr >= 200 else 0)
            ol = pmlat.olat(M.m, t_dec, M.sc)  # TTL counts from arrival
            o = M.post(tok, price, t_dec, min(t_dec + ol + ttl, M.end), ("out",), against)
            if o is None:
                continue
            busy[tok] = o["t_cxl"]
            c = cnt.setdefault(label, [0, 0, 0])
            c[0] += 1
            c[1] += int(o["status"] == "ok")
            if not o["fills"]:
                continue
            c[2] += 1
            out.append(M.rec(label, o, "entry"))
            if hedge_cap is not None:  # resting hedge on the other token, pair cost <= cap, until 10 s before the close
                tf = int(o["fills"][0]["x"])
                td = tf + M.sc["book"]
                if td < M.end - 10000:
                    ih = M.seen(td)
                    otok = 1 - tok
                    ask_o = (10000 - int(M.b[ih])) if otok == 1 else int(M.k[ih])
                    ph = int((hedge_cap * 1e4 - price) // 100 * 100)
                    if ask_o:
                        ph = min(ph, ask_o - 100)
                    h = M.post(otok, ph, td, M.end - 10000, ())
                    if h is not None:
                        ch = cnt.setdefault(label + "#hedge", [0, 0, 0])
                        ch[0] += 1
                        ch[1] += int(h["status"] == "ok")
                        if h["fills"]:
                            ch[2] += 1
                            out.append(M.rec(label, h, "hedge", entry_px=price))


def run_pair(M, cfg, out, cnt, pairs):
    alt = cfg.split("|")[1] == "y"
    sc = M.sc
    pa = M.pa(50)
    anyalt = np.sort(np.concatenate([union(pa, 0), union(pa, 1)])) if pa else np.zeros(0, np.int64)
    t_free = M.e * 1000
    pid = 0
    while True:
        t = next_stable(M.C, t_free)
        t_dec = t + sc["book"]
        if t_dec > M.end - 20000:
            break
        i = M.seen(t_dec)
        if i < 0 or M.b[i] == 0 or M.k[i] == 0:
            j = int(np.searchsorted(M.x, t_dec, "right"))
            if j >= len(M.x):
                break
            t_free = int(M.x[j])
            continue
        legs = []
        for tok in (0, 1):
            o = M.post(tok, best_bid(M, i, tok), t_dec, M.end - 20000, (), anyalt if alt else None)
            legs.append(o)
        if alt and all(legs):  # a leg fill seen before the alt cancel decision keeps the other leg resting
            tf = [int(o["fills"][0]["x"]) if o["fills"] else None for o in legs]
            for a in (0, 1):
                o, other = legs[a], legs[1 - a]
                if tf[a] is not None and not other["fills"] and tf[a] + sc["book"] < other["t_cxl"] - sc["cancel"]:
                    legs[1 - a] = M.post(1 - a, other["price"], t_dec, M.end - 20000, (), None)
                    break
        c = cnt.setdefault(cfg, [0, 0, 0])
        nf = 0
        for o in legs:
            if o is None:
                continue
            c[0] += 1
            c[1] += int(o["status"] == "ok")
            if o["fills"]:
                c[2] += 1
                nf += 1
        for n, o in enumerate(legs):
            if o is not None and o["fills"]:
                out.append(M.rec(cfg, o, f"leg{n}", pid=pid, both=int(nf == 2)))
        pairs.append(dict(cfg=cfg, e=M.e, pid=pid, legs_filled=nf))
        pid += 1
        done = max(o["t_cxl"] for o in legs if o is not None) if any(legs) else t_dec
        t_free = max(done - sc["book"], t_dec - sc["book"] + 1)


def run_pre(M, cfg, out, cnt):
    _, lvl, cx = cfg.split("|")
    lvl = int(lvl)
    if not len(M.x) or M.x[0] >= M.e * 1000:
        return
    t_dec = int(M.x[0]) + M.sc["book"]
    pa = M.pa(50)
    for tok in (0, 1):
        against = (union(pa, 1) if tok == 0 else union(pa, 0)) if cx == "alt" else None
        o = M.post(tok, lvl, t_dec, M.end, (), against)
        if o is None:
            continue
        c = cnt.setdefault(cfg, [0, 0, 0])
        c[0] += 1
        c[1] += int(o["status"] == "ok")
        if o["fills"]:
            c[2] += 1
            out.append(M.rec(cfg, o, "entry"))


def run_calm(M, cfg, out, cnt):
    _, imb, off, ttl = cfg.split("|")
    imb, off = float(imb), int(off)
    sc = M.sc
    pa = M.pa(50)
    against = {0: union(pa, 1), 1: union(pa, 0)}
    t_free = [M.e * 1000, M.e * 1000]
    while True:
        cand = []
        for tok in (0, 1):
            if t_free[tok] is not None:
                t = next_stable(M.C, t_free[tok]) + sc["book"]
                if t <= M.end - 20000:
                    cand.append((t, tok))
                else:
                    t_free[tok] = None
        if not cand:
            break
        t_dec, tok = min(cand)
        i = M.seen(t_dec)
        nxt_row = int(np.searchsorted(M.x, t_dec - sc["book"], "right"))
        if i < 0 or M.b[i] == 0 or M.k[i] == 0:
            t_free[tok] = int(M.x[nxt_row]) if nxt_row < len(M.x) else None
            continue
        tot = M.bs[i] + M.ks[i]
        im = (M.bs[i] - M.ks[i]) / tot if tot > 0 else 0.0
        if (im if tok == 0 else -im) <= imb:
            t_free[tok] = int(M.x[nxt_row]) if nxt_row < len(M.x) else None
            continue
        price = best_bid(M, i, tok) + 100 * off
        ol = pmlat.olat(M.m, t_dec, sc)
        t_end = min(t_dec + ol + int(ttl), M.end - 20000) if ttl != "close" else M.end - 20000
        o = M.post(tok, price, t_dec, t_end, (), against[tok])
        if o is None:
            t_free[tok] = int(M.x[nxt_row]) if nxt_row < len(M.x) else None
            continue
        c = cnt.setdefault(cfg, [0, 0, 0])
        c[0] += 1
        c[1] += int(o["status"] == "ok")
        if o["fills"]:
            c[2] += 1
            out.append(M.rec(cfg, o, "entry"))
        t_free[tok] = max(o["t_cxl"] - sc["book"], t_dec - sc["book"] + 1)


def run_imb(M, cfg, out, cnt):
    """Item 8: at a seen book row where a token's top-of-book imbalance > IMB and its mid rose >= 0.25c over the last 1 s,
    BUY 5 at its best bid (pess queue). Rest until its imbalance < 0 (seen), TTL after arrival, or 10 s before the close;
    ALT=y also cancels on an alt move against. One live order per token."""
    _, imb, ttl, alt = cfg.split("|")
    imb, ttl = float(imb), int(ttl)
    sc, x, b, k = M.sc, M.x, M.b, M.k
    ok = (b > 0) & (k > 0)
    tot = M.bs + M.ks
    im_up = np.where(tot > 0, (M.bs - M.ks) / np.where(tot > 0, tot, 1), 0.0)
    mid = (b + k) / 2.0
    j = np.searchsorted(x, x - 1000, "right") - 1
    prev_ok = (j >= 0) & ok[np.clip(j, 0, None)]
    dmid = np.where(prev_ok, mid - mid[np.clip(j, 0, None)], 0.0)
    pa = M.pa(50) if alt == "y" else None
    against = {0: union(pa, 1), 1: union(pa, 0)} if pa is not None else {0: None, 1: None}
    t0, t1 = M.e * 1000, M.end - 10000
    for tok in (0, 1):
        sim = im_up if tok == 0 else -im_up
        dm = dmid if tok == 0 else -dmid
        cand = np.flatnonzero(ok & (sim > imb) & (dm >= 25))
        busy = -1
        for r in cand:
            t_dec = int(x[r]) + sc["book"]
            if not t0 <= t_dec < t1 or t_dec < busy:
                continue
            price = best_bid(M, r, tok)
            ol = pmlat.olat(M.m, t_dec, sc)
            t_end = min(t_dec + ol + ttl, t1)
            # imbalance < 0 on a later seen row cancels: pass it as an extra signal list
            later = np.flatnonzero((sim[r + 1:] < 0) & ok[r + 1:])
            sig = [int(x[r + 1 + later[0]])] if len(later) else []
            a = against[tok]
            ag = np.sort(np.concatenate([a, np.array(sig, np.int64)])) if a is not None else np.array(sig, np.int64)
            o = M.post(tok, price, t_dec, t_end, (), ag)
            if o is None:
                continue
            busy = o["t_cxl"]
            c = cnt.setdefault(cfg, [0, 0, 0])
            c[0] += 1
            c[1] += int(o["status"] == "ok")
            if o["fills"]:
                c[2] += 1
                out.append(M.rec(cfg, o, "entry"))


def run_cfg(M, cfg, out, cnt, pairs):
    kind = cfg.split("|")[0]
    if kind == "alt":
        run_alt(M, cfg, out, cnt)
    elif kind == "hedge":
        _, cap, rest = cfg.split("|", 2)
        run_alt(M, rest, out, cnt, hedge_cap=float(cap), label=cfg)
    elif kind == "pair":
        run_pair(M, cfg, out, cnt, pairs)
    elif kind == "pre":
        run_pre(M, cfg, out, cnt)
    elif kind == "calm":
        run_calm(M, cfg, out, cnt)
    elif kind == "imb":
        run_imb(M, cfg, out, cnt)


def main():
    day, scn, tf = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sc = pmlat.SC[scn]
    cfgs = (ALL5 if tf == 5 else ALL15) if len(sys.argv) <= 4 or sys.argv[4] == "all" else sys.argv[4].split(";")
    tag = os.environ.get("F9_TAG", "")
    base = pmlat.OUT / f"f9-{scn}-{tf}m-{day}{tag}"
    cols = ["cfg", "e", "role", "tok", "px", "filled", "tfill", "tleft", "olat", "t_live", "mo1000", "mo5000", "settle_c",
            "entry_px", "pid", "both"]
    fo = gzip.open(f"{base}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    cnt, pairs, nm = {}, [], 0
    W = pmlat.winners([])
    gens = {a: pmlat.hours(day, a, 5, with_act=False) for a in ALTS}
    cur = {a: (None, {}) for a in ALTS}
    for h, mk in pmlat.hours(day, "btc", tf, with_act=False):
        for a in ALTS:
            while cur[a][0] is None or cur[a][0] < h:
                try:
                    cur[a] = next(gens[a])
                except StopIteration:
                    cur[a] = (99, {})
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        out = []
        for e, m in mk.items():
            # alt 5m windows inside this target window (5m target: the same epoch)
            alt_mk = [mm for a in ALTS if cur[a][0] == h for ee, mm in cur[a][1].items() if e <= ee < e + tf * 60]
            M = Mkt(m, W.get(pmlat.slug(m["key"])), alt_mk, sc)
            nm += 1
            for cfg in cfgs:
                run_cfg(M, cfg, out, cnt, pairs)
        w.writerows(out)
        fo.flush()
        print(day, scn, tf, h, len(out), flush=True)
    fo.close()
    with open(f"{base}-orders.csv", "w") as f:
        f.write("cfg,orders,accepted,filled_orders,markets\n")
        for c, v in cnt.items():
            f.write(f"{c},{v[0]},{v[1]},{v[2]},{nm}\n")
    with open(f"{base}-pairs.csv", "w") as f:
        f.write("cfg,e,pid,legs_filled\n")
        for p in pairs:
            f.write(f"{p['cfg']},{p['e']},{p['pid']},{p['legs_filled']}\n")
    print("wrote", base, len(cnt), "cfgs", nm, "markets")


if __name__ == "__main__":
    main()
