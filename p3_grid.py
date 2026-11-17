"""15m study, Part 3 grid (maker ideas for a slower book), post-only BUY 5, pess queue, strict fills via PMLAT_STRICT, hold to settlement.
Configs:
  hedge|CAP          entry = F3 join on a new level, rest 60 s, no pull (Part 2 'bid|60000|none'); after its fill at p, rest a bid on the
                     other token at min(CAP - p, its ask - 1 tick) until 30 s before the close (completion = both legs filled)
  pair|STAB          patient pair: after STAB ms with the best bid and ask unchanged, bid both tokens at their best bids; no cancel until
                     30 s before the close; next pair only when both orders are done (one side never leads by more than one order)
  calm|STAB|IMB|OFF|TTL  after STAB ms of unchanged best prices, join (OFF 0) or sit one tick below (OFF -1) the best bid of the token
                     with own imbalance > IMB (IMB 'any' = the token whose bid size is larger); no pull; TTL after arrival; one order per token
  pre|LVL|CXL        at the earliest pre-open book row, BUY both tokens at LVL; CXL alt (ETH/SOL/XRP/DOGE 5m mid >= 0.5c against) or never
  mr|LOOK|THR|DIR    every 5 s: a token's mid moved >= THR over LOOK ms -> DIR mom: bid the token that rose; rev: bid the one that fell;
                     at its best bid, rest 60 s; one order per token
Usage: PMLAT_MAXDAY=... PMLAT_STRICT=1 python p3_grid.py DAY SCEN [asset tf] -> out/p2/p3-{asset}{tf}-{scn}-{day}-strict.csv.gz
"""
import csv
import gzip
import itertools
import os
import sys
from pathlib import Path

import numpy as np

import f9_live  # noqa: F401
import pmlat
import dsim
import f3_maker
from f8_grid import next_stable
from f9_grid import Mkt, best_bid, union

OUT = Path(__file__).resolve().parent / "out" / "p2"
ALTS = ("eth", "sol", "xrp", "doge")
CFGS = (["hedge|0.99", "hedge|0.97", "hedge|0.95", "pair|1000", "pair|3000"]
        + ["calm|" + "|".join(c) for c in itertools.product(("1000", "3000"), ("any", "0.6"), ("0", "-1"), ("15000", "60000"))]
        + ["pre|" + "|".join(c) for c in itertools.product(("5000", "4900", "4800"), ("alt", "never"))]
        + ["mr|" + "|".join(c) for c in itertools.product(("30000", "60000", "120000"), ("200", "500"), ("mom", "rev"))])
MOS = (1000, 5000, 30000, 60000)
LAST = 30000


def own_imb(M, i, tok):
    tot = M.bs[i] + M.ks[i]
    im = (M.bs[i] - M.ks[i]) / tot if tot > 0 else 0.0
    return im if tok == 0 else -im


def rec(M, cfg, o, role, imb0=None, **kw):
    tf_ = int(o["fills"][0]["x"])
    tok, px = o["tok"], o["price"]
    jf = int(np.searchsorted(M.x, tf_, "right")) - 1
    r = dict(cfg=cfg, e=M.e, role=role, tok=tok, px=px, filled=float(sum(z["size"] for z in o["fills"])), tfill=tf_, tleft=M.end - tf_,
             olat=o["t_arr"] - o["t_dec"], t_live=o["t_cxl"] - o["t_arr"], imb_entry=None if imb0 is None else round(imb0, 3),
             imb_fill=round(own_imb(M, jf, tok), 3) if jf >= 0 else None, rebate_c=round(100 * 0.014 * (px / 1e4) * (1 - px / 1e4), 4))
    for h in MOS:
        mm = dsim.mid(M.m, tf_ + h, tok) if tf_ + h < M.end - LAST else None
        r[f"mo{h}"] = None if mm is None else (mm - px) / 100
    if M.winner is not None:
        r["settle_c"] = 100 * ((1.0 if (M.winner == "UP") == (tok == 0) else 0.0) - px / 1e4)
    r.update(kw)
    return r


def count(cnt, cfg, o):
    c = cnt.setdefault(cfg, [0, 0, 0])
    c[0] += 1
    c[1] += int(o["status"] == "ok")
    c[2] += int(bool(o["fills"]))


def run_hedge(M, cfg, out, cnt):
    cap = float(cfg.split("|")[1])
    sc = M.sc
    ups, dns = f3_maker.events(M.m)
    pid = 0
    for side, rows in ((0, ups), (1, dns)):
        tok, busy = side, -1
        for r0 in rows:
            x0 = int(M.x[r0])
            if not M.e * 1000 <= x0 <= M.end - LAST:
                continue
            t_dec = x0 + sc["book"]
            if t_dec < busy:
                continue
            i = M.seen(t_dec)
            q = int(M.b[r0]) if side == 0 else int(M.k[r0])
            if i < 0 or (int(M.b[i]) if side == 0 else int(M.k[i])) != q:
                continue
            price = q if tok == 0 else 10000 - q
            ol = pmlat.olat(M.m, t_dec, sc)
            o = M.post(tok, price, t_dec, min(t_dec + ol + 60000, M.end - LAST), ())
            if o is None:
                continue
            busy = o["t_cxl"]
            count(cnt, cfg, o)
            if not o["fills"]:
                continue
            tf_ = int(o["fills"][0]["x"])
            td = tf_ + sc["book"]
            hrow = None
            if td < M.end - LAST:
                ih = M.seen(td)
                ask_o = (10000 - int(M.b[ih])) if tok == 0 else int(M.k[ih])  # ask of the other token
                ph = int((cap * 1e4 - price) // 100 * 100)
                if ask_o:
                    ph = min(ph, ask_o - 100)
                h = M.post(1 - tok, ph, td, M.end - LAST, ())
                if h is not None:
                    count(cnt, cfg + "#hedge", h)
                    if h["fills"]:
                        hrow = rec(M, cfg, h, "hedge", pid=pid, entry_px=price)
            out.append(rec(M, cfg, o, "entry", own_imb(M, i, tok), pid=pid, done=int(hrow is not None), tleft0=M.end - tf_))
            if hrow:
                hrow["done"] = 1
                hrow["tleft0"] = M.end - tf_
                out.append(hrow)
            pid += 1


def run_pair(M, cfg, out, cnt):
    stab = int(cfg.split("|")[1])
    sc = M.sc
    t_free, pid = M.e * 1000, 0
    while True:
        t = next_stable(M.C, t_free, stab)
        t_dec = t + sc["book"]
        if t_dec > M.end - LAST - 5000:
            break
        i = M.seen(t_dec)
        if i < 0 or M.b[i] == 0 or M.k[i] == 0:
            j = int(np.searchsorted(M.x, t_dec, "right"))
            if j >= len(M.x):
                break
            t_free = int(M.x[j])
            continue
        legs = [M.post(tok, best_bid(M, i, tok), t_dec, M.end - LAST, ()) for tok in (0, 1)]
        nf = sum(bool(o and o["fills"]) for o in legs)
        for n, o in enumerate(legs):
            if o is None:
                continue
            count(cnt, cfg, o)
            if o["fills"]:
                out.append(rec(M, cfg, o, f"leg{n}", own_imb(M, i, o["tok"]), pid=pid, done=int(nf == 2)))
        pid += 1
        done = max(o["t_cxl"] for o in legs if o is not None) if any(legs) else t_dec
        # both legs rest to the close unless filled: the pair is done when the later of the two fills/expiries happens
        ends = []
        for o in legs:
            if o is None:
                continue
            ends.append(int(o["fills"][-1]["x"]) if o["fills"] and sum(z["size"] for z in o["fills"]) >= 5 - 1e-9 else o["t_cxl"])
        done = max(ends) if ends else done
        t_free = max(done - sc["book"], t_dec - sc["book"] + 1)


def run_calm(M, cfg, out, cnt):
    _, stab, imb, off, ttl = cfg.split("|")
    stab, off, ttl = int(stab), int(off), int(ttl)
    sc = M.sc
    t_free = [M.e * 1000, M.e * 1000]
    while True:
        cand = []
        for tok in (0, 1):
            if t_free[tok] is not None:
                t = next_stable(M.C, t_free[tok], stab) + sc["book"]
                if t <= M.end - LAST:
                    cand.append((t, tok))
                else:
                    t_free[tok] = None
        if not cand:
            break
        t_dec, tok = min(cand)
        i = M.seen(t_dec)
        nxt = int(np.searchsorted(M.x, t_dec - sc["book"], "right"))
        skip = i < 0 or M.b[i] == 0 or M.k[i] == 0
        if not skip:
            im = own_imb(M, i, tok)  # imb 'any': join the token whose bid side is the larger one
            skip = (im <= 0) if imb == "any" else (im <= float(imb))
        if skip:  # re-evaluate 2 s later (not on every book event)
            t_free[tok] = t_dec - sc["book"] + 2000
            continue
        price = best_bid(M, i, tok) + 100 * off
        ol = pmlat.olat(M.m, t_dec, sc)
        o = M.post(tok, price, t_dec, min(t_dec + ol + ttl, M.end - LAST), ())
        if o is None:
            t_free[tok] = int(M.x[nxt]) if nxt < len(M.x) else None
            continue
        count(cnt, cfg, o)
        if o["fills"]:
            out.append(rec(M, cfg, o, "entry", own_imb(M, i, tok)))
        t_free[tok] = max(o["t_cxl"] - sc["book"], t_dec - sc["book"] + 1)


def run_pre(M, cfg, out, cnt):
    _, lvl, cx = cfg.split("|")
    lvl = int(lvl)
    if not len(M.x) or M.x[0] >= M.e * 1000:
        return
    t_dec = int(M.x[0]) + M.sc["book"]
    pa = M.pa(50)
    for tok in (0, 1):
        against = (union(pa, 1) if tok == 0 else union(pa, 0)) if cx == "alt" else None
        o = M.post(tok, lvl, t_dec, M.end - LAST, (), against)
        if o is None:
            continue
        count(cnt, cfg, o)
        if o["fills"]:
            i0 = M.seen(t_dec)
            out.append(rec(M, cfg, o, "entry", own_imb(M, i0, tok) if i0 >= 0 else None))


def run_mr(M, cfg, out, cnt):
    _, look, thr, dr = cfg.split("|")
    look, thr = int(look), int(thr)
    sc = M.sc
    busy = [-1, -1]
    for t_dec in range(M.e * 1000 + look, M.end - LAST - 60000, 5000):
        i = M.seen(t_dec)
        j = M.seen(t_dec - look)
        if i < 0 or j < 0 or M.b[i] == 0 or M.k[i] == 0 or M.b[j] == 0 or M.k[j] == 0:
            continue
        dm = ((int(M.b[i]) + int(M.k[i])) - (int(M.b[j]) + int(M.k[j]))) / 2  # UP mid change
        if abs(dm) < thr:
            continue
        rose = 0 if dm > 0 else 1  # token that rose
        tok = rose if dr == "mom" else 1 - rose
        if t_dec < busy[tok]:
            continue
        o = M.post(tok, best_bid(M, i, tok), t_dec, t_dec + pmlat.olat(M.m, t_dec, sc) + 60000, ())
        if o is None:
            continue
        busy[tok] = o["t_cxl"]
        count(cnt, cfg, o)
        if o["fills"]:
            out.append(rec(M, cfg, o, "entry", own_imb(M, i, tok)))


RUN = {"hedge": run_hedge, "pair": run_pair, "calm": run_calm, "pre": run_pre, "mr": run_mr}


def main():
    day, scn = sys.argv[1], sys.argv[2]
    asset = sys.argv[3] if len(sys.argv) > 3 else "btc"
    tf = int(sys.argv[4]) if len(sys.argv) > 4 else 15
    cfgs = os.environ["P3_CFGS"].split(";") if os.environ.get("P3_CFGS") else CFGS
    sc = pmlat.SC[scn]
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"p3-{asset}{tf}-{scn}-{day}{'-strict' if f3_maker.STRICT else ''}{os.environ.get('P3_TAG', '')}"
    cols = ["cfg", "e", "role", "tok", "px", "filled", "tfill", "tleft", "olat", "t_live", "imb_entry", "imb_fill", "rebate_c",
            "mo1000", "mo5000", "mo30000", "mo60000", "settle_c", "pid", "done", "tleft0", "entry_px"]
    fo = gzip.open(f"{base}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    cnt, nm = {}, 0
    W = pmlat.winners([])
    g5 = {a: pmlat.hours(day, a, 5, with_act=False) for a in ALTS}
    cur = {}

    def adv(key, gen, h):
        while cur.get(key, (None,))[0] is None or cur[key][0] < h:
            try:
                cur[key] = next(gen)
            except StopIteration:
                cur[key] = (99, {})
        return cur[key][1] if cur[key][0] == h else {}

    for h, mk in pmlat.hours(day, asset, tf, with_act=False):
        a5 = {a: adv(a, g5[a], h) for a in ALTS}
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        out = []
        for e, m in mk.items():
            alt = [mm for a in ALTS for ee, mm in a5[a].items() if e <= ee < e + tf * 60]
            M = Mkt(m, W.get(pmlat.slug(m["key"])), alt, sc)
            nm += 1
            for cfg in cfgs:
                RUN[cfg.split("|")[0]](M, cfg, out, cnt)
        w.writerows(out)
        fo.flush()
        print(day, scn, h, len(out), flush=True)
    fo.close()
    with open(f"{base}-orders.csv", "w") as f:
        f.write("cfg,orders,accepted,filled_orders,markets\n")
        for c, v in cnt.items():
            f.write(f"{c},{v[0]},{v[1]},{v[2]},{nm}\n")
    print("wrote", base, len(cnt), "cfgs", nm, "markets")


if __name__ == "__main__":
    main()
