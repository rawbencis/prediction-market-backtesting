"""15m study, Part 2: one-table check that fees still kill the taker ideas on BTC 15m. dsim taker fills (FAK at arrival + hold
against the top level, >= 5 shown after same-ms prints; fee 0.07 p (1-p) on every leg). Decide at event + book (+9).
Rules (one entry per market per 30 s unless noted):
  F5   top-of-book |imbalance| >= 0.8 and spread <= 1c (sampled every 250 ms): buy the favoured token at its ask + 1c cap
  F1   a print >= $50 (size x token price): buy the print's token (its direction) at its ask + 1c cap
  F6   at 60 s left (the burst second on 5m): buy the favourite at its ask + 1c cap (once per market)
  LB-X late breakout: in the last 60 s a token's ask first reaches X (60/70/80c) not reached earlier in the window: buy at ask + 5c cap (once)
Exits: taker sell 5 s after the fill (FAK at the bid, fee), or hold to settlement.
Usage: PMLAT_MAXDAY=... python p2_taker.py DAY SCEN -> out/p2/taker-{scn}-{day}.csv.gz
"""
import csv
import gzip
import sys
from pathlib import Path

import numpy as np

import f9_live  # noqa: F401
import pmlat
import dsim

OUT = Path(__file__).resolve().parent / "out" / "p2"


def ask_of(B, i, tok):
    return int(B["k"][i]) if tok == 0 else (10000 - int(B["b"][i]) if B["b"][i] else 0)


def trade(m, sc, rule, t_ev, tok, cap_c, end, winner, out):
    B = m["book"]
    t_dec = t_ev + sc["book"]
    i = int(np.searchsorted(B["x"], t_ev, "right")) - 1
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return False
    a = ask_of(B, i, tok)
    if not 100 <= a <= 9800:
        return False
    tm = t_dec + pmlat.olat(m, t_dec, sc) + sc["hold"]
    if tm >= end:
        return False
    f = dsim.taker(m, int(tm), tok, a + cap_c * 100, 5.0)
    if f is None:
        out.append(dict(rule=rule, e=m["key"][2], tok=tok, filled=0))
        return True
    px, fee_c = f["px"], 100 * f["fee"] / f["size"]
    r = dict(rule=rule, e=m["key"][2], tok=tok, filled=1, px=px, tleft=end - f["x"], fee_c=round(fee_c, 3))
    tx = f["x"] + 5000
    if tx < end - 2000:
        g = dsim.taker(m, int(tx + sc["hold"]), tok, 100, 5.0, sell=True)
        if g is not None:
            r["exit5_c"] = (g["px"] - px) / 100 - fee_c - 100 * g["fee"] / g["size"]
    if winner is not None:
        r["settle_c"] = 100 * ((1.0 if (winner == "UP") == (tok == 0) else 0.0)) - px / 100 - fee_c
    out.append(r)
    return True


def run_market(m, sc, winner, out):
    B, P = m["book"], m["pr"]
    e = m["key"][2]
    t0, end = e * 1000, (e + 900) * 1000
    x = B["x"].astype(np.int64)
    b, k = B["b"].astype(np.int64), B["k"].astype(np.int64)
    bs, ks = B["bs"].astype(float), B["ks"].astype(float)
    # F5
    last = -10**15
    for t in range(t0 + 1000, end - 30000, 250):
        if t - last < 30000:
            continue
        i = int(np.searchsorted(x, t, "right")) - 1
        if i < 0 or b[i] == 0 or k[i] == 0 or k[i] - b[i] > 100 or bs[i] + ks[i] <= 0:
            continue
        im = (bs[i] - ks[i]) / (bs[i] + ks[i])
        if abs(im) >= 0.8 and trade(m, sc, "F5", t, 0 if im > 0 else 1, 1, end, winner, out):
            last = t
    # F1: prints >= $50 (size x token price); a taker buy of UP or a taker sell of DOWN pushes UP up
    last = -10**15
    for j in range(len(P["x"])):
        t = int(P["x_print"][j]) if "x_print" in P else int(P["x"][j])
        if not t0 <= t < end - 30000 or t - last < 30000:
            continue
        up_px = int(P["up"][j])
        tok = 1 if P["hb"][j] else 0  # hit the UP bid = DOWN pressure
        usd = float(P["sz"][j]) * ((up_px if tok == 0 else 10000 - up_px) / 1e4)
        if usd >= 50 and trade(m, sc, "F1", t, tok, 1, end, winner, out):
            last = t
    # F6: favourite at 60 s left
    t = end - 60000
    i = int(np.searchsorted(x, t, "right")) - 1
    if i >= 0 and b[i] and k[i]:
        trade(m, sc, "F6", t, 0 if (b[i] + k[i]) / 2 >= 5000 else 1, 1, end, winner, out)
    # late breakout
    for X in (6000, 7000, 8000):
        done = False
        for tok in (0, 1):
            a = np.where(tok == 0, np.where(k > 0, k, 0), np.where(b > 0, 10000 - b, 0))
            pre = (x >= t0) & (x < end - 60000)
            if (a[pre] >= X).any():
                continue
            late = np.flatnonzero((x >= end - 60000) & (x < end - 5000) & (a >= X))
            if len(late) and not done:
                done = trade(m, sc, f"LB-{X // 100}", int(x[late[0]]), tok, 5, end, winner, out)


def main():
    day, scn = sys.argv[1], sys.argv[2]
    sc = pmlat.SC[scn]
    OUT.mkdir(parents=True, exist_ok=True)
    W = pmlat.winners([])
    out = []
    for h, mk in pmlat.hours(day, "btc", 15, with_act=False):
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            run_market(m, sc, W.get(pmlat.slug(m["key"])), out)
        print(day, scn, h, len(out), flush=True)
    cols = ["rule", "e", "tok", "filled", "px", "tleft", "fee_c", "exit5_c", "settle_c"]
    with gzip.open(OUT / f"taker-{scn}-{day}.csv.gz", "wt") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print("wrote", len(out))


if __name__ == "__main__":
    main()
