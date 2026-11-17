"""15m study, Part 2: F3 join-maker on BTC 15m (the live `bid` / `vol` arms), pess queue, hold to settlement.
Entry: a new best level forms (f3_maker.events; >= 5 shares, >= 30 s left); decide at x + book if the level is still best;
post-only BUY 5 at that price (join, pess: behind its displayed size). One live order per side.
Config = entry | hold | pull
  entry  bid : any new level;  vol : own-side top-of-book imbalance >= 0.6 at the decision
  hold   15000 / 60000 / 180000 ms after arrival, or last30 (until 30 s before the close)
  pull   none | alt15 (>= 3 ETH/SOL/XRP/DOGE 15m books move 1c against within 500 ms) | btc5 (the concurrent BTC 5m book's UP
         mid moves 1c against within 500 ms) | out (someone improves past us)
Per fill: mid markouts at 1/5/30/60 s (only while a two-sided book exists and > 30 s remain), settle, rebate (20% of the
taker fee 0.07 p (1-p), separate column), own imbalance at entry and at the fill.
Usage: PMLAT_MAXDAY=... python p2_join.py DAY SCEN [asset] [tf] -> out/p2/p2-{asset}{tf}-{scen}-{day}{-strict}.csv.gz (+ -orders.csv)
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
from f9_grid import Mkt, alt_per_alt, triggers, union

OUT = Path(__file__).resolve().parent / "out" / "p2"
ALTS = ("eth", "sol", "xrp", "doge")
CFGS = ["|".join(c) for c in itertools.product(("bid", "vol"), ("15000", "60000", "180000", "last30"), ("none", "alt15", "btc5", "out"))]
MOS = (1000, 5000, 30000, 60000)


def own_imb(M, i, tok):
    tot = M.bs[i] + M.ks[i]
    im = (M.bs[i] - M.ks[i]) / tot if tot > 0 else 0.0
    return im if tok == 0 else -im


def run(M, cfgs, sig, out, cnt):
    sc = M.sc
    ups, dns = f3_maker.events(M.m)
    for cfg in cfgs:
        entry, hold, pull = cfg.split("|")
        for side, rows in ((0, ups), (1, dns)):
            tok = side
            busy = -1
            for r0 in rows:
                x0 = int(M.x[r0])
                if not M.e * 1000 <= x0 <= M.end - 30000:
                    continue
                t_dec = x0 + sc["book"]
                if t_dec < busy:
                    continue
                i = M.seen(t_dec)
                q = int(M.b[r0]) if side == 0 else int(M.k[r0])
                if i < 0 or (int(M.b[i]) if side == 0 else int(M.k[i])) != q:
                    continue
                imb0 = own_imb(M, i, tok)
                if entry == "vol" and imb0 < 0.6:
                    continue
                price = q if tok == 0 else 10000 - q
                ol = pmlat.olat(M.m, t_dec, sc)
                t_end = M.end - 30000 if hold == "last30" else min(t_dec + ol + int(hold), M.end - 30000)
                against = sig[pull][tok] if pull in ("alt15", "btc5") else None
                o = M.post(tok, price, t_dec, t_end, ("out",) if pull == "out" else (), against)
                if o is None:
                    continue
                busy = o["t_cxl"]
                c = cnt.setdefault(cfg, [0, 0, 0])
                c[0] += 1
                c[1] += int(o["status"] == "ok")
                if not o["fills"]:
                    continue
                c[2] += 1
                tf_ = int(o["fills"][0]["x"])
                jf = int(np.searchsorted(M.x, tf_, "right")) - 1
                r = dict(cfg=cfg, e=M.e, tok=tok, px=price, filled=float(sum(z["size"] for z in o["fills"])), tfill=tf_,
                         tleft=M.end - tf_, olat=o["t_arr"] - t_dec, t_live=o["t_cxl"] - o["t_arr"], imb_entry=round(imb0, 3),
                         imb_fill=round(own_imb(M, jf, tok), 3) if jf >= 0 else None,
                         rebate_c=round(100 * 0.2 * 0.07 * (price / 1e4) * (1 - price / 1e4), 4))
                for h in MOS:
                    ok = tf_ + h < M.end - 30000
                    mm = dsim.mid(M.m, tf_ + h, tok) if ok else None
                    r[f"mo{h}"] = None if mm is None else (mm - price) / 100
                if M.winner is not None:
                    r["settle_c"] = 100 * ((1.0 if (M.winner == "UP") == (tok == 0) else 0.0) - price / 1e4)
                out.append(r)


def main():
    day, scn = sys.argv[1], sys.argv[2]
    asset = sys.argv[3] if len(sys.argv) > 3 else "btc"
    tf = int(sys.argv[4]) if len(sys.argv) > 4 else 15
    cfgs = os.environ["P2_CFGS"].split(";") if os.environ.get("P2_CFGS") else CFGS
    sc = pmlat.SC[scn]
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"p2-{asset}{tf}-{scn}-{day}{'-strict' if f3_maker.STRICT else ''}{os.environ.get('P2_TAG', '')}"
    cols = ["cfg", "e", "tok", "px", "filled", "tfill", "tleft", "olat", "t_live", "imb_entry", "imb_fill", "rebate_c",
            "mo1000", "mo5000", "mo30000", "mo60000", "settle_c"]
    fo = gzip.open(f"{base}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    cnt, nm = {}, 0
    W = pmlat.winners([])
    others = [a for a in ("btc",) + ALTS if a != asset]
    g15 = {a: pmlat.hours(day, a, tf, with_act=False) for a in others}
    g5 = pmlat.hours(day, asset, 5, with_act=False)
    cur = {}

    def adv(key, gen, h):
        while cur.get(key, (None,))[0] is None or cur[key][0] < h:
            try:
                cur[key] = next(gen)
            except StopIteration:
                cur[key] = (99, {})
        return cur[key][1] if cur[key][0] == h else {}

    for h, mk in pmlat.hours(day, asset, tf, with_act=False):
        oth = {a: adv(a, g15[a], h) for a in others}
        own5 = adv("own5", g5, h)
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        out = []
        for e, m in mk.items():
            alt = [oth[a][e] for a in others if e in oth[a]]
            pa = alt_per_alt(alt, 100)
            b5 = alt_per_alt([mm for ee, mm in own5.items() if e <= ee < e + tf * 60], 100)
            sig = {"alt15": {0: triggers(pa, 3, 1), 1: triggers(pa, 3, 0)},  # against a bid on UP: alts fall
                   "btc5": {0: union(b5, 1) if b5 else np.zeros(0, np.int64), 1: union(b5, 0) if b5 else np.zeros(0, np.int64)}}
            M = Mkt(m, W.get(pmlat.slug(m["key"])), alt, sc)
            nm += 1
            run(M, cfgs, sig, out, cnt)
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
