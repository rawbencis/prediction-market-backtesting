"""F8: exploratory maker grid around F7i, BTC 5m, post-only BUY of UP (side 0) / DOWN (side 1), 5 shares, pess queue.

Both sides of a market are simulated in time order with one live order per side, so a pair cap can use the inventory
held at decision time. Latencies from pmlat.SC[scn] (live_lean: post +55 if the book's best bid/ask were unchanged for
the previous 1 s, else +157; cancel +136; book +9). A cancel never lands before order-on-book + cancel latency (ackc).

Config = entry | offset | pulls | ttl | cap
  entry  new    : a new best level forms (f3_maker.events); decide at x0 + book if the level is still best (F7i entry)
         stable : decide as soon as the best bid and ask have been unchanged for 1 s (seen + book), whenever the side is free
  offset +1/0/-1/-2 ticks from the best bid of the token we buy (+1 needs a spread >= 2 ticks)
  pulls  alt     : an adverse alt-book move (ETH/SOL/XRP/DOGE 5m UP mid moves >= 50 bp in 500 ms against us; f7_altpull)
         alt+out : alt, or our side's best moves past max(our price, best at entry)
         none    : TTL only
         (all: cancel when the book goes empty)
  ttl    5000 / 30000 ms after arrival, or 'close' = 20 s before the window ends
  cap    off / 0.99: skip a post if (avg price of the other token already held + our price) > 0.99
Output: filled orders only -> out/pmlat/f8-{scn}-{day}[-strict].csv.gz ; order counts -> ...-orders.csv
Usage: python f8_grid.py DAY SCEN [hours|all]   env F8_CFGS='id;id' restricts configs, PMLAT_STRICT=1 strict fills
"""

import csv
import gzip
import itertools
import os
import sys

import numpy as np

import pmlat  # noqa: I001
import dsim
import f3_maker
from f7_altpull import ALTS, alt_events

ENTRIES = ("new", "stable")
OFFS = (1, 0, -1, -2)
PULLS = ("alt", "alt+out", "none")
TTLS = ("5000", "30000", "close")
CAPS = ("off", "0.99")
ALL = ["|".join(map(str, c)) for c in itertools.product(ENTRIES, OFFS, PULLS, TTLS, CAPS)]
SIZE = 5.0
INF = np.iinfo(np.int64).max


def next_stable(C, t0, need=1000):
    """Earliest t >= t0 with no price-change time in (t - need, t]. C sorted change times."""
    while True:
        j = int(np.searchsorted(C, t0, "right")) - 1
        if j < 0 or C[j] <= t0 - need:
            return t0
        cand = int(C[j]) + need
        nxt = int(C[j + 1]) if j + 1 < len(C) else INF
        if nxt > cand:
            return cand
        t0 = nxt


def run_cfg(m, sc, cfg, winner, altev, P, out, cnt):
    entry, off, pulls, ttl, cap = cfg.split("|")
    off = int(off)
    cap = None if cap == "off" else float(cap)
    B, x, b, k, C, e, end_ms = P["B"], P["x"], P["b"], P["k"], P["C"], P["e"], P["end"]
    n = len(x)
    last_dec = end_ms - 20000
    busy = [-1, -1]
    held = []  # (fill x, tok, size, price)

    def post(side, t_dec):
        """-> t_cxl if an order was sent, else None."""
        i = int(np.searchsorted(x, t_dec, "right")) - 1
        if i < 0 or b[i] == 0 or k[i] == 0:
            return None
        bb, kk = int(b[i]), int(k[i])
        if off == 1 and kk - bb < 200:
            return None
        tok = side
        price = (bb if side == 0 else 10000 - kk) + 100 * off
        if price < 100 or price > 9900:
            return None
        if cap is not None:
            sh = sum(z[2] for z in held if z[1] == 1 - tok and z[0] <= t_dec)
            if sh > 0:
                avg = sum(z[2] * z[3] for z in held if z[1] == 1 - tok and z[0] <= t_dec) / sh
                if (avg + price) / 1e4 > cap + 1e-9:
                    return None
        ol = pmlat.olat(m, t_dec, sc)
        t_arr = t_dec + ol
        t_end = t_arr + int(ttl) if ttl != "close" else end_ms - 20000
        if t_end <= t_arr:
            return None
        # pulls: scan book rows seen before t_end
        t_cxl = t_end + sc["cancel"]
        i0 = i + 1
        i1 = int(np.searchsorted(x, t_end - sc["book"], "left"))
        if i1 > i0:
            bs, ks = b[i0:i1], k[i0:i1]
            trig = (bs == 0) & (ks == 0)
            if "out" in pulls:
                trig |= (bs > max(price, bb)) if side == 0 else (ks < min(10000 - price, kk))
            j = np.flatnonzero(trig)
            if len(j):
                t_cxl = min(t_cxl, int(x[i0 + j[0]]) + sc["book"] + sc["cancel"])
        if "alt" in pulls:
            a = altev[side]
            q = int(np.searchsorted(a, t_dec, "right"))
            if q < len(a):
                t_cxl = min(t_cxl, int(a[q]) + sc["book"] + sc["cancel"])
        t_cxl = max(t_cxl, t_arr + sc["cancel"])  # ackc: the cancel needs the ack
        res = f3_maker.MAKER(m, t_arr, tok, price, SIZE, t_cxl, model="pess")
        c = cnt.setdefault(cfg, [0, 0, 0])
        c[0] += 1
        c[1] += int(ol == sc.get("order_fast", -1))
        f = res["fills"]
        filled = float(sum(z["size"] for z in f))
        if filled > 0:
            c[2] += 1
            tf = int(f[0]["x"])
            for z in f:
                held.append((int(z["x"]), tok, float(z["size"]), price))
            mm = dsim.mid(m, tf + 1000, tok) if tf + 1000 < end_ms else None
            out.append(dict(cfg=cfg, e=e, side=side, tok=tok, px=price, filled=filled, tfill=tf, tleft=end_ms - tf,
                            olat=ol, t_live=t_cxl - t_arr, mo1000=None if mm is None else (mm - price) / 100,
                            settle_c=None if winner is None else 100 * ((1.0 if (winner == "UP") == (tok == 0) else 0.0) - price / 1e4)))
        return t_cxl

    if entry == "new":
        for t_dec, side, r0 in P["new"]:
            if t_dec < busy[side]:
                continue
            i = int(np.searchsorted(x, t_dec, "right")) - 1
            q0 = int(b[r0]) if side == 0 else int(k[r0])
            if (int(b[i]) if side == 0 else int(k[i])) != q0:
                continue  # level gone / improved when we see it
            tc = post(side, t_dec)
            if tc is not None:
                busy[side] = tc
    else:
        t_free = [e * 1000, e * 1000]
        nxt = [None, None]
        while True:
            for s in (0, 1):
                if nxt[s] is None and t_free[s] is not None:
                    t = next_stable(C, t_free[s])
                    nxt[s] = t + sc["book"] if t + sc["book"] <= last_dec else None
                    if nxt[s] is None:
                        t_free[s] = None
            live = [s for s in (0, 1) if nxt[s] is not None]
            if not live:
                break
            s = min(live, key=lambda z: nxt[z])
            t_dec = nxt[s]
            nxt[s] = None
            tc = post(s, t_dec)
            if tc is not None:
                t_free[s] = max(tc - sc["book"], t_dec - sc["book"] + 1)
            else:  # nothing posted: re-check at the next book row
                j = int(np.searchsorted(x, t_dec, "right"))
                t_free[s] = int(x[j]) if j < n else None


def prep(m):
    B = m["book"]
    x = B["x"].astype(np.int64)
    b, k = B["b"].astype(np.int64), B["k"].astype(np.int64)
    e = m["key"][2]
    end_ms = (e + m["key"][1] * 60) * 1000
    ch = np.r_[True, (b[1:] != b[:-1]) | (k[1:] != k[:-1])]
    ups, dns = f3_maker.events(m)
    new = []
    for side, rows in ((0, ups), (1, dns)):
        for r0 in rows:
            x0 = int(x[r0])
            if e * 1000 <= x0 <= end_ms - 20000:
                new.append((x0 + 9, side, int(r0)))  # book seen +9 in every scenario used here
    new.sort()
    return dict(B=B, x=x, b=b, k=k, C=x[ch], e=e, end=end_ms, new=new)


def main():
    day, scn = sys.argv[1], sys.argv[2]
    hrs = range(24) if len(sys.argv) <= 3 or sys.argv[3] == "all" else [int(h) for h in sys.argv[3].split(",")]
    sc = pmlat.SC[scn]
    assert sc["book"] == 9.0
    cfgs = os.environ["F8_CFGS"].split(";") if os.environ.get("F8_CFGS") else ALL
    sfx = ("-strict" if f3_maker.STRICT else "") + os.environ.get("F8_TAG", "")
    outf = pmlat.OUT / f"f8-{scn}-{day}{sfx}.csv.gz"
    cols = ["cfg", "e", "side", "tok", "px", "filled", "tfill", "tleft", "olat", "t_live", "mo1000", "settle_c"]
    fo = gzip.open(outf, "wt")
    w = csv.DictWriter(fo, fieldnames=cols)
    w.writeheader()
    cnt, nm = {}, 0
    W = pmlat.winners([])
    alts = ALTS
    gens = {a: pmlat.hours(day, a, 5, hrs=hrs, with_act=False) for a in alts}
    alt_cur = {a: (None, {}) for a in alts}
    for h, mk in pmlat.hours(day, "btc", 5, hrs=hrs, with_act=False):
        for a in alts:
            while alt_cur[a][0] is None or alt_cur[a][0] < h:
                try:
                    alt_cur[a] = next(gens[a])
                except StopIteration:
                    alt_cur[a] = (99, {})
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        out = []
        for e, m in mk.items():
            altm = [alt_cur[a][1][e] for a in alts if alt_cur[a][0] == h and e in alt_cur[a][1]]
            altev = alt_events(altm, 50)
            P = prep(m)
            nm += 1
            for cfg in cfgs:
                run_cfg(m, sc, cfg, W.get(pmlat.slug(m["key"])), altev, P, out, cnt)
        w.writerows(out)
        fo.flush()
        print(day, scn, h, len(out), flush=True)
    fo.close()
    with open(str(outf).replace(".csv.gz", "-orders.csv"), "w") as f:
        f.write("cfg,orders,n_fast,n_filled,markets\n")
        for c, v in cnt.items():
            f.write(f"{c},{v[0]},{v[1]},{v[2]},{nm}\n")
    print("wrote", outf, len(cnt), "cfgs", nm, "markets")


if __name__ == "__main__":
    main()
