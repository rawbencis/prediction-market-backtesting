"""Family 1 (+2 via wallet column, +4-ish fade): taker entry right behind a trade print, dsim fills.

For each print with usd >= MIN_USD (event table from f1_events.py), for side in (follow, fade):
  decide at t_d = t_p + book delay (we see the print), BUY the token in that direction FAK at the ask seen at t_d
  + 1 tick (cap), matching at t_d + order + hold (dsim.taker: top level only, >= 5 shown, others first on ties).
  Exits (all computed per fill):
    settle            hold to settlement (no exit fee)
    tk{H}             taker SELL at the bid, decided at entry match + book + H ms (dsim.taker sell=True, any price);
                      if no bid with >= 5 shown -> hold to settlement
    mk{X}             post-only exit X ticks above the entry price (BUY of the other token at 1 - (px + X)), arriving
                      entry match + book + order; pess queue; cancel after 10 s then taker exit (as tk), else settle
Out: out/pmlat/f1sim-{asset}{tf}-{scenario}-{day}.csv.gz, one row per (event, side) with fill and pnl per exit.
"""

import gzip
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001  (sets sys.path for dsim)
import dsim

MIN_USD = 20.0
TK = (1000, 5000, 30000)
MK = (100, 300)
MK_TTL = 10000


def taker_exit(m, t_dec, tok, sc, end_ms):
    t = t_dec + sc["order"] + sc["hold"]
    if t >= end_ms:
        return None
    return dsim.taker(m, t, tok, 0, size=5.0, sell=True)


def sim_event(m, ev, side, sc, winner):
    e = int(ev.e)
    end_ms = (e + 300) * 1000 if m["key"][1] == 5 else (e + 900) * 1000
    tok = (0 if ev.dir == 1 else 1) if side == "follow" else (1 if ev.dir == 1 else 0)
    td = int(ev.tp) + sc["book"]
    B, i = m["book"], dsim.state(m, td, strict=False)
    if i < 0 or B["k"][i] == 0 or B["b"][i] == 0:
        return None
    ask = int(B["k"][i]) if tok == 0 else 10000 - int(B["b"][i])
    tmatch = td + sc["order"] + sc["hold"]
    if tmatch >= end_ms - 1000:
        return None
    f = dsim.taker(m, tmatch, tok, ask + 100, size=5.0)
    r = dict(e=e, j=int(ev.j), side=side, tok=tok, ask_seen=ask, filled=int(f is not None))
    if f is None:
        return r
    px, fee_in = f["px"], f["fee"] / f["size"]
    pay = None if winner is None else (1.0 if (winner == "UP") == (tok == 0) else 0.0)
    r.update(px=px, fee_in=fee_in, settle=None if pay is None else pay - px / 1e4 - fee_in)
    seen = tmatch + sc["book"]
    for H in TK:
        g = taker_exit(m, seen + H, tok, sc, end_ms)
        if g is None:
            r[f"tk{H}"] = r["settle"]
            r[f"tk{H}_x"] = 0
        else:
            r[f"tk{H}"] = (g["px"] - px) / 1e4 - fee_in - g["fee"] / g["size"]
            r[f"tk{H}_x"] = 1
    for X in MK:
        q = px + X
        r[f"mk{X}_x"] = 0
        if q >= 9900:
            r[f"mk{X}"] = r["settle"]
            continue
        t_arr = seen + sc["order"]
        t_cxl = min(seen + MK_TTL, end_ms - 2000) + sc["cancel"]
        res = dsim.maker(m, t_arr, 1 - tok, 10000 - q, 5.0, t_cxl, model="pess")
        filled = sum(z["size"] for z in res["fills"])
        if res["status"] == "ok" and filled >= 5.0 - 1e-6:
            r[f"mk{X}"] = X / 1e4 - fee_in
            r[f"mk{X}_x"] = 1
        else:  # partial or no fill: the remainder exits as taker at the cancel time (else settle); pro rata
            g = taker_exit(m, t_cxl - sc["cancel"], tok, sc, end_ms)
            rest = (r["settle"] if g is None else (g["px"] - px) / 1e4 - fee_in - g["fee"] / g["size"])
            fr = filled / 5.0 if res["status"] == "ok" else 0.0
            r[f"mk{X}"] = None if rest is None else fr * (X / 1e4 - fee_in) + (1 - fr) * rest
    return r


def main():
    day, scn = sys.argv[1], sys.argv[2]
    asset = sys.argv[3] if len(sys.argv) > 3 else "btc"
    tf = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    sc = pmlat.SC[scn]
    ev = pd.read_csv(pmlat.OUT / f"ev-{asset}{tf}-{day}.csv.gz", usecols=["e", "j", "tp", "dir", "usd"])
    ev = ev[ev.usd >= MIN_USD]
    W = pmlat.winners([pmlat.slug((asset, tf, int(e))) for e in ev.e.unique()])
    out = []
    for h, mk in pmlat.hours(day, asset, tf, with_act=False):
        for e, m in mk.items():
            sub = ev[ev.e == e]
            w = W.get(pmlat.slug(m["key"]))
            for r in sub.itertuples():
                for side in ("follow", "fade"):
                    x = sim_event(m, r, side, sc, w)
                    if x is not None:
                        out.append(x)
        print(day, scn, h, len(out), flush=True)
    f = pmlat.OUT / f"f1sim-{asset}{tf}-{scn}-{day}.csv.gz"
    with gzip.open(f, "wt") as fo:
        pd.DataFrame(out).to_csv(fo, index=False)
    print("wrote", f, len(out))


if __name__ == "__main__":
    main()
