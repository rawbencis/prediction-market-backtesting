"""Late-dump study, job 2: in the last T s of a BTC 5m window, when one token's best bid >= X, post-only BUY 5 of it at its best bid
(join; = selling 5 of the other token at 1 - bid). One order per window, rest until the close; variant: pull when that bid falls
2 ticks (seen +9, cancel +cancel, never before order-on-book + cancel). Placebo: the same trigger, but BUY the other token at its best bid.
Scenario latencies from pmlat.SC (live: post +55 if the BTC best bid/ask were unchanged 1 s, else +190; cancel +90), pess queue
(behind all displayed size at our price on landing), fill model from PMLAT_STRICT. Settle-only P&L (late mids are survivorship-biased).
Config = side|T|X|pull : side fav / plac ; T 120/60/30/15 s ; X 9500/9700/9800/9900 ; pull none / drop2
Per order: queue ahead at landing (displayed size at our price), shares traded at our price (prints on our side) between landing and
the close, fill, seconds left at the fill, settle.
Usage: PMLAT_MAXDAY=... PMLAT_STRICT=1 python ld_sim.py DAY SCEN -> out/ld/ld-{scn}-{day}.csv.gz
"""
import csv
import gzip
import itertools
import sys
from pathlib import Path

import numpy as np

import f9_live  # noqa: F401
import pmlat
import f3_maker

OUT = Path(__file__).resolve().parent / "out" / "ld"
CFGS = ["|".join(c) for c in itertools.product(("fav", "plac"), ("120", "60", "30", "15"), ("9500", "9700", "9800", "9900"), ("none", "drop2"))]
SIZE = 5.0


def run_market(m, sc, winner, out):
    B, P = m["book"], m["pr"]
    x = B["x"].astype(np.int64)
    b, k = B["b"].astype(np.int64), B["k"].astype(np.int64)
    bs, ks = B["bs"].astype(float), B["ks"].astype(float)
    e = m["key"][2]
    end = (e + 300) * 1000
    up_bid, dn_bid = b, np.where(k > 0, 10000 - k, 0)
    for cfg in CFGS:
        side, T, X, pull = cfg.split("|")
        T, X = int(T), int(X)
        t0 = end - T * 1000
        # first moment in the last T s (as seen) when a token's best bid >= X
        i0 = int(np.searchsorted(x, t0 - sc["book"], "right")) - 1
        cand = [i0] if i0 >= 0 else []
        cand += list(range(i0 + 1, int(np.searchsorted(x, end - 1000 - sc["book"], "right"))))
        trig = None
        for i in cand:
            if b[i] == 0 and k[i] == 0:
                continue
            if up_bid[i] >= X or dn_bid[i] >= X:
                trig = i
                break
        if trig is None:
            continue
        i = trig
        fav = 0 if up_bid[i] >= X else 1
        t_dec = max(int(x[i]) + sc["book"], t0)
        tok = fav if side == "fav" else 1 - fav
        price = int(up_bid[i] if tok == 0 else dn_bid[i])
        if price < 100:
            continue
        t_arr = t_dec + pmlat.olat(m, t_dec, sc)
        if t_arr >= end:
            continue
        t_cxl = end + sc["cancel"]
        if pull == "drop2":  # the bid we joined falls 2 ticks (as seen)
            mine = up_bid if tok == 0 else dn_bid
            j = np.flatnonzero((x > x[i]) & (x + sc["book"] < end) & (mine <= price - 200) & ((b > 0) | (k > 0)))
            if len(j):
                t_cxl = max(int(x[j[0]]) + sc["book"] + sc["cancel"], t_arr + sc["cancel"])
        res = f3_maker.MAKER(m, t_arr, tok, price, SIZE, t_cxl, model="pess")
        # queue at landing: displayed size at our price (our side of the UP book: UP bid for tok 0, UP ask = 1 - DOWN bid for tok 1)
        ia = int(np.searchsorted(x, t_arr, "right")) - 1
        lvl = price if tok == 0 else 10000 - price
        if ia >= 0:
            top = int(b[ia]) if tok == 0 else int(k[ia])
            ahead = float(bs[ia] if tok == 0 else ks[ia]) if top == lvl else (0.0 if (top < lvl if tok == 0 else (top > lvl or top == 0)) else np.nan)
        else:
            ahead = np.nan
        hit_side = (tok == 0)  # prints that hit our level: taker sells UP (hb True) when we bid UP; taker buys UP (hb False) when we bid DOWN
        w = (P["x"] > t_arr) & (P["x"] < min(t_cxl, end)) & (P["up"] == lvl) & (P["hb"] == hit_side)
        traded = float(P["sz"][w].sum())
        f = res["fills"]
        filled = float(sum(z["size"] for z in f))
        r = dict(cfg=cfg, e=e, tok=tok, fav=int(tok == fav), price=price, tleft_dec=(end - t_dec) / 1000, olat=t_arr - t_dec,
                 status=res["status"], ahead=ahead, traded=traded, pulled=int(t_cxl < end), filled=filled,
                 tleft_fill=(end - int(f[0]["x"])) / 1000 if f else None, winner=winner)
        if winner is not None:
            pay = 1.0 if (winner == "UP") == (tok == 0) else 0.0
            r["won"] = int(pay == 1.0)
            r["settle_c"] = 100 * (pay - price / 1e4)
        out.append(r)


def main():
    day, scn = sys.argv[1], sys.argv[2]
    sc = pmlat.SC[scn]
    OUT.mkdir(parents=True, exist_ok=True)
    W = pmlat.winners([])
    cols = ["cfg", "e", "tok", "fav", "price", "tleft_dec", "olat", "status", "ahead", "traded", "pulled", "filled", "tleft_fill",
            "winner", "won", "settle_c"]
    fo = gzip.open(OUT / f"ld-{scn}-{day}{'-strict' if f3_maker.STRICT else ''}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    n = 0
    for h, mk in pmlat.hours(day, "btc", 5, with_act=False):
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        out = []
        for e, m in mk.items():
            run_market(m, sc, W.get(pmlat.slug(m["key"])), out)
        w.writerows(out)
        n += len(out)
        print(day, scn, h, n, flush=True)
    fo.close()
    print("wrote", n)


if __name__ == "__main__":
    main()
