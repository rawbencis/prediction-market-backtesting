"""Family 3: fast maker joining a newly formed best level, pulled on book-only triggers (dsim maker queue models).

Event: a book row where the UP bid rises (new best bid) or the UP ask falls (new best ask), both sides valid, the new
level shows >= 5 shares, and the window has >= 20 s left. We decide at x + book (level must still be best there) and
post-only BUY 5 shares at that price (UP bid side: BUY UP at b; UP ask side: BUY DOWN at 1 - k), arriving + order.
Queue: pess (ahead = displayed size at arrival, only prints at our price advance it), prop (cancels advance it pro rata),
front (ahead 0, reference only).
Pull triggers (each seen at row x + book, cancel lands + cancel; cancels are not held):
  dep    our level's displayed size < DEP * its size at our arrival (others are pulling / being eaten)
  opp    the opposite best moves toward us (UP ask falls when we bid; UP bid rises when we offer) by >= 1 tick
  out    someone improves past our price (we are no longer best)
  ttl    TTL ms after arrival
Policy: one live order per side; new levels are skipped while it is live (gate = cancel time).\nFill accounting: first 5 shares. Per fill: markout of the token mid at +100 ms / 1 s / 5 s / 30 s (in c/share), settle PnL,
and an exit plan: post-only exit at px + 1 tick (TTL 10 s, same model) then taker at the bid, else settle.
Usage: python f3_maker.py DAY SCEN [asset tf hours|all mode(join|improve)] -> out/pmlat/f3-{asset}{tf}-{scen}-{day}.csv.gz
"""

import gzip
import os
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001  (sets sys.path for dsim)
import dsim

DEP = 0.5
STRICT = os.environ.get("PMLAT_STRICT") == "1"  # dsim_strict.maker_strict: through-prints fill only their own size
SIZE = float(os.environ.get("PMLAT_SIZE", "5"))  # order size (shares); output files get a -s{SIZE} suffix if != 5
TTLS = (5000,)
MODELS = ("pess", "prop", "front")
EXIT_TTL = 10000


def events(m):
    B = m["book"]
    b, k, bs, ks = (B[c].astype(np.int64) if c in "bk" else B[c] for c in ("b", "k", "bs", "ks"))
    ok = (b > 0) & (k > 0)
    prev_ok = np.r_[False, ok[:-1]]
    pb, pk = np.r_[0, b[:-1]], np.r_[0, k[:-1]]
    up = ok & prev_ok & (b > pb) & (bs >= 5)
    dn = ok & prev_ok & (k < pk) & (ks >= 5)
    return np.flatnonzero(up), np.flatnonzero(dn)


def pull_time(m, r0, side, q, sz0, t_arr, ttl, sc, rules):
    """First trigger row after our decision; returns the cancel arrival time (exchange ms)."""
    B = m["book"]
    t_dec = int(B["x"][r0]) + sc["book"]
    t_end = t_arr + ttl
    i = dsim.state(m, t_dec, strict=False) + 1
    n = len(B["x"])
    while i < n:
        x = int(B["x"][i])
        if x + sc["book"] >= t_end:
            break
        b, k = int(B["b"][i]), int(B["k"][i])
        if b == 0 and k == 0:
            return x + sc["book"] + sc["cancel"]
        if side == 0:  # we bid q on UP
            mine, size, opp_move, out = b, B["bs"][i], k < int(B["k"][r0]), b > q
        else:  # we offer q on UP (BUY DOWN)
            mine, size, opp_move, out = k, B["ks"][i], b > int(B["b"][r0]), k < q
        if ("out" in rules and out) or ("opp" in rules and opp_move) or (
            "dep" in rules and mine == q and size < DEP * sz0
        ):
            return x + sc["book"] + sc["cancel"]
        i += 1
    return t_end + sc["cancel"]


def tok_mid(m, t, tok):
    v = dsim.mid(m, t, tok)
    return None if v is None else v


def run_market(m, sc, winner, rules_list):
    B = m["book"]
    e = m["key"][2]
    end_ms = (e + m["key"][1] * 60) * 1000
    ups, dns = events(m)
    out = []
    for side, rows in ((0, ups), (1, dns)):
        busy = {}  # (rules, ttl) -> time our previous order on this side is gone (one live order per side)
        for r0 in rows:
            x0 = int(B["x"][r0])
            if x0 > end_ms - 20000 or x0 < e * 1000:
                continue
            q = int(B["b"][r0]) if side == 0 else int(B["k"][r0])
            sz0 = float(B["bs"][r0] if side == 0 else B["ks"][r0])
            t_dec = x0 + sc["book"]
            i = dsim.state(m, t_dec, strict=False)
            if (int(B["b"][i]) if side == 0 else int(B["k"][i])) != q:
                continue  # level already gone / improved when we see it
            imb = (float(B["bs"][i]) - float(B["ks"][i])) / max(float(B["bs"][i]) + float(B["ks"][i]), 1e-9)
            if MODE == "improve":  # step one tick inside the spread: alone at a new best price
                if int(B["k"][i]) - int(B["b"][i]) < 200:
                    continue
                q = q + 100 if side == 0 else q - 100
                sz0 = 0.0
            tok = 0 if side == 0 else 1
            price = q if tok == 0 else 10000 - q
            t_arr = t_dec + sc["order"]
            for rules in rules_list:
                for ttl in TTLS:
                    if t_dec < busy.get((rules, ttl), -1):
                        continue
                    t_cxl = pull_time(m, r0, side, q, sz0, t_arr, ttl, sc, rules)
                    busy[(rules, ttl)] = t_cxl
                    for model in MODELS:
                        res = MAKER(m, t_arr, tok, price, SIZE, t_cxl, model=model)
                        f = res["fills"]
                        rec = dict(e=e, x0=x0, spr=int(B["k"][r0]) - int(B["b"][r0]), tleft0=end_ms - x0, imb=imb, mode=MODE, side=side, tok=tok, px=price, sz0=sz0, rules="+".join(rules), ttl=ttl,
                                   model=model, status=res["status"], t_live=t_cxl - t_arr,
                                   filled=float(sum(z["size"] for z in f)))
                        if rec["filled"] > 0:
                            tf = f[0]["x"]
                            rec["tfill"] = tf
                            rec["tleft"] = end_ms - tf
                            for h in (100, 1000, 5000, 30000):
                                mm = tok_mid(m, tf + h, tok) if tf + h < end_ms else None
                                rec[f"mo{h}"] = None if mm is None else (mm - price) / 100  # c/share
                            if winner is not None:
                                pay = 1.0 if (winner == "UP") == (tok == 0) else 0.0
                                rec["settle_c"] = 100 * (pay - price / 1e4)
                            # exit: post at price + 100 (BUY other token at 1 - (price + 100)), TTL then taker
                            qx = price + 100
                            seen = tf + sc["book"]
                            ex = None
                            if qx < 9900:
                                ta, tc = seen + sc["order"], min(seen + EXIT_TTL, end_ms - 2000) + sc["cancel"]
                                rr = MAKER(m, ta, 1 - tok, 10000 - qx, 5.0, tc, model=model)
                                if rr["status"] == "ok" and sum(z["size"] for z in rr["fills"]) >= 5 - 1e-6:
                                    ex = 1.0
                                else:
                                    g = dsim.taker(m, tc - sc["cancel"] + sc["order"] + sc["hold"], tok, 0, 5.0, sell=True) \
                                        if tc - sc["cancel"] + sc["order"] + sc["hold"] < end_ms else None
                                    if g is not None:
                                        ex = (g["px"] - price) / 100 - 100 * g["fee"] / g["size"]
                            rec["exit_c"] = ex if ex is not None else rec.get("settle_c")
                            rec["exit_mk"] = int(ex == 1.0)
                        out.append(rec)
    return out


OUTF, NW, COLS = [None], [0], [None]


def flush(rows):
    """Append rows to OUTF (gz csv) and clear them; keeps memory flat. rows None: just make sure the file exists."""
    import csv as _csv

    if rows is None:
        if NW[0] == 0:
            gzip.open(OUTF[0], "wt").close()
        return
    if not rows:
        return
    if COLS[0] is None:
        cols = []
        for r in rows:
            cols += [k for k in r if k not in cols]
        COLS[0] = cols + [c for c in ("tfill", "tleft", "mo100", "mo1000", "mo5000", "mo30000", "settle_c", "exit_c", "exit_mk", "thr", "n_alts") if c not in cols]
    with gzip.open(OUTF[0], "at" if NW[0] else "wt") as fo:
        w = _csv.DictWriter(fo, fieldnames=COLS[0], extrasaction="ignore")
        if NW[0] == 0:
            w.writeheader()
        w.writerows(rows)
    NW[0] += len(rows)
    rows.clear()


RULES = (("out", "ttl"), ("out", "opp", "dep", "ttl"))


def MAKER(*a, **k):
    if STRICT:
        import dsim_strict

        return dsim_strict.maker_strict(*a, **k)
    return dsim.maker(*a, **k)


MODE = "join"


def main():
    global MODE
    day, scn = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 6:
        MODE = sys.argv[6]
    asset = sys.argv[3] if len(sys.argv) > 3 else "btc"
    tf = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    hrs = range(24) if len(sys.argv) <= 5 or sys.argv[5] == "all" else [int(h) for h in sys.argv[5].split(",")]
    sc = pmlat.SC[scn]
    out = []
    sfx = ("" if SIZE == 5 else f"-s{SIZE:g}") + ("-strict" if STRICT else "")
    OUTF[0] = pmlat.OUT / f"f3{'' if MODE == 'join' else 'i'}-{asset}{tf}-{scn}-{day}{sfx}.csv.gz"
    W = None
    for h, mk in pmlat.hours(day, asset, tf, hrs=hrs, with_act=False):
        if W is None:
            W = pmlat.winners([])
        for e, m in mk.items():
            W.update(pmlat.winners([pmlat.slug(m["key"])]) if pmlat.slug(m["key"]) not in W else {})
            out += run_market(m, sc, W.get(pmlat.slug(m["key"])), RULES)
        flush(out)
        print(day, scn, h, NW[0], flush=True)
    flush(None)
    print("wrote", OUTF[0], NW[0])


if __name__ == "__main__":
    main()
