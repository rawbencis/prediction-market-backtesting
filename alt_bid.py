"""Live `bid` join-maker (as live on BTC 5m since 2026-11-17 21:16 UTC) on recorded 5m books, any asset.

Rule (per window, both tokens; tok 0 = UP, tok 1 = DOWN):
  trigger : a new best level (f3_maker.events: UP bid rises -> UP side; UP ask falls -> DOWN side), level >= 5 shares,
            >= 20 s left, spread (UP ask - UP bid) >= 2 ticks (>= 2c) on that book row.
  order   : post-only BUY 5 at that side's best bid (join; pess queue = behind the displayed size on arrival).
            Decision at row + book (+9 ms), on book + 190 ms (`live`; the 55 ms fast path never applies because the
            triggering row itself changed the best). Strict fills (dsim_strict).
  rest    : until fully filled, 60 s after arrival, or 10 s before the close; cancel lands +90 ms (never before
            arrival + 90). Then 2 s before that side may re-enter (from full fill / cancel landing / reject).
  limits  : one live order per side; per side <= 10 shares (filled + this order); lead: (this side filled + 5) -
            (other side filled + resting) <= 5; second leg: if the other side has filled or resting shares, this
            price + their average price <= 0.99. State known at the decision time (fills with time <= decision).
  Hold to settlement.
Outputs per run: out/alts/ab-{asset}5-{day}.csv.gz (one row per order sent, with fills) and -mk.csv (all windows).
Usage: PMLAT_MAXDAY=... PMLAT_STRICT=1 python alt_bid.py DAY ASSET
"""
import csv
import gzip
import sys
from pathlib import Path

import numpy as np

import f9_live  # noqa: F401  (registers SC['live'])
import pmlat
import dsim
import f3_maker
from f9_grid import Mkt

OUT = Path(__file__).resolve().parent / "out" / "alts"
SIZE, CAP_SIDE, LEAD, PAIR = 5.0, 10.0, 5.0, 9900
HOLD, CLOSE_STOP, LEFT_MIN, WAIT, MIN_SPR = 60000, 10000, 20000, 2000, 200
COLS = ["e", "tok", "x0", "t_dec", "t_arr", "t_cxl", "t_done", "status", "px", "spr", "lvl_sz", "lvl_sz_arr", "filled",
        "n_ev", "first_sz", "tfill", "tleft", "mo1000", "mo5000", "settle_c", "rebate_c"]


def known(orders, t):
    """-> (filled shares, resting shares, committed cost sum in 1e-4 $) of one side's orders as known at t."""
    f = r = cost = 0.0
    for o in orders:
        if o["t_dec"] > t:
            continue
        ff = sum(z["size"] for z in o["fills"] if z["x"] <= t)
        f += ff
        cost += ff * o["px"]
        if t < o["t_done"]:
            r += SIZE - ff
            cost += (SIZE - ff) * o["px"]
    return f, r, cost


def run_market(M, winner):
    sc = M.sc
    ups, dns = f3_maker.events(M.m)
    ev = sorted([(int(M.x[r]), 0, int(r)) for r in ups] + [(int(M.x[r]), 1, int(r)) for r in dns])
    orders = {0: [], 1: []}
    free = [-1, -1]
    rows = []
    for x0, tok, r0 in ev:
        if not M.e * 1000 <= x0 <= M.end - LEFT_MIN:
            continue
        if M.k[r0] - M.b[r0] < MIN_SPR:
            continue
        t_dec = x0 + int(sc["book"])
        if t_dec < free[tok]:
            continue
        price = int(M.b[r0]) if tok == 0 else 10000 - int(M.k[r0])
        f_me, r_me, _ = known(orders[tok], t_dec)
        f_ot, r_ot, c_ot = known(orders[1 - tok], t_dec)
        if f_me + r_me + SIZE > CAP_SIDE + 1e-9:
            continue
        if (f_me + r_me + SIZE) - (f_ot + r_ot) > LEAD + 1e-9:
            continue
        if f_ot + r_ot > 1e-9 and price + c_ot / (f_ot + r_ot) > PAIR + 1e-9:
            continue
        ol = pmlat.olat(M.m, t_dec, sc)
        t_end = min(t_dec + ol + HOLD, M.end - CLOSE_STOP)
        o = M.post(tok, price, t_dec, t_end, ())
        if o is None:
            continue
        fills = o["fills"]
        filled = float(sum(z["size"] for z in fills))
        if o["status"] != "ok":
            t_done = o["t_arr"]
        elif filled >= SIZE - 1e-9:
            t_done = int(fills[-1]["x"])
        else:
            t_done = o["t_cxl"]
        free[tok] = t_done + WAIT
        rec = dict(e=M.e, tok=tok, x0=x0, t_dec=t_dec, t_arr=o["t_arr"], t_cxl=o["t_cxl"], t_done=t_done, px=price,
                   status=o["status"], fills=fills)
        orders[tok].append(rec)
        ia = M.seen(o["t_arr"] + sc["book"])
        r = dict(e=M.e, tok=tok, x0=x0, t_dec=t_dec, t_arr=o["t_arr"], t_cxl=o["t_cxl"], t_done=t_done,
                 status=o["status"], px=price, spr=int(M.k[r0] - M.b[r0]),
                 lvl_sz=float(M.bs[r0] if tok == 0 else M.ks[r0]),
                 lvl_sz_arr=float(M.bs[ia] if tok == 0 else M.ks[ia]) if ia >= 0 else None,
                 filled=filled, n_ev=len(fills), rebate_c=round(100 * 0.2 * 0.07 * (price / 1e4) * (1 - price / 1e4), 4))
        if fills:
            tf = int(fills[0]["x"])
            r.update(first_sz=float(fills[0]["size"]), tfill=tf, tleft=M.end - tf)
            for h in (1000, 5000):
                mm = dsim.mid(M.m, tf + h, tok) if tf + h < M.end else None
                r[f"mo{h}"] = None if mm is None else (mm - price) / 100
            if winner is not None:
                r["settle_c"] = 100 * ((1.0 if (winner == "UP") == (tok == 0) else 0.0) - price / 1e4)
        rows.append(r)
    return rows


def main():
    day, asset = sys.argv[1], sys.argv[2]
    assert f3_maker.STRICT, "run with PMLAT_STRICT=1"
    sc = pmlat.SC["live"]
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"ab-{asset}5-{day}"
    fo = gzip.open(f"{base}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    mko = open(f"{base}-mk.csv", "w")
    mko.write("e,winner,rows\n")
    W = pmlat.winners([])
    n = 0
    for h, mk in pmlat.hours(day, asset, 5, with_act=False):
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            win = W.get(pmlat.slug(m["key"]))
            mko.write(f"{e},{win or ''},{len(m['book']['x'])}\n")
            rows = run_market(Mkt(m, win, [], sc), win)
            w.writerows(rows)
            n += len(rows)
        fo.flush()
        mko.flush()
        print(day, asset, h, n, flush=True)
    fo.close()
    mko.close()
    print("wrote", base, n)


if __name__ == "__main__":
    main()
