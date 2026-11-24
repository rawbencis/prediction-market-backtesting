"""D3: the 2026-11-18 15m taker table (p2_taker.py rules) at today's fees, `live` timing, any asset and timeframe.

Rules (Polymarket signals only; one entry per market per 30 s unless noted):
  F5   top-of-book |imbalance| >= 0.8 and spread <= 1c (sampled every 250 ms): buy the favoured token, cap ask + 1c
  F1   a print >= $50 (size x token price): buy the print's token, cap ask + 1c
  F6   at 60 s left: buy the favourite, cap ask + 1c (once per market)
  LB-X in the last 60 s a token's ask first reaches X (60/70/80c), not reached earlier in the window: cap ask + 5c (once)
Execution `live` (f9_live SC['live']): decide at event + 9 ms; the FAK lands +55 ms if the market's best bid and ask
were unchanged over the previous 1 s, else +190 ms, and matches after the 150 ms hold against the top level with >= 5
shown (dsim.taker, same-ms prints first). Pessimistic fill: the paid price (or better) must still show >= 5 shares
100 ms after the match, else no fill. Fee: FEE p (1-p) per share per taker leg (0.05 BTC, 0.01 alts; maker rebate 0).
Per fill: settle (win - paid - fee), mid markouts at 1 s / 5 s / 30 s after the fill (gross of fee, only while the
book is two-sided and > 30 s remain at the markout time), and a 5 s taker exit (FAK at the bid, fee on both legs).
Usage: PYTHONPATH=~/overnight-wt:~/dublin-retest/repo python d3_taker.py DAY ASSET TF -> out/d3/taker-ASSET-TF-DAY.csv.gz
"""
import csv
import gzip
import sys
from pathlib import Path

import numpy as np

import f9_live  # noqa: F401  (registers SC['live'])
import pmlat
import dsim

OUT = Path(__file__).resolve().parent.parent / "out" / "d3"


def ask_of(B, i, tok):
    return int(B["k"][i]) if tok == 0 else (10000 - int(B["b"][i]) if B["b"][i] else 0)


def mid_tok(m, t, tok):
    B = m["book"]
    i = int(np.searchsorted(B["x"], t, "right")) - 1
    if i < 0 or not (0 < B["b"][i] < B["k"][i] < 10000):
        return None
    mu = (int(B["b"][i]) + int(B["k"][i])) / 2
    return mu if tok == 0 else 10000 - mu


def pess_ok(m, t, tok, px):
    B = m["book"]
    i = int(np.searchsorted(B["x"], t, "right")) - 1
    if i < 0:
        return False
    if tok == 0:
        return 0 < B["k"][i] <= px and B["ks"][i] >= 5
    return 0 < B["b"][i] and 10000 - int(B["b"][i]) <= px and B["bs"][i] >= 5


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
        out.append(dict(rule=rule, e=m["key"][2], tok=tok, filled=0, ask=a))
        return True
    px, fee_c = f["px"], 100 * f["fee"] / f["size"]
    r = dict(rule=rule, e=m["key"][2], tok=tok, filled=1, ask=a, px=px, tleft=end - f["x"], fee_c=round(fee_c, 3),
             pess=int(pess_ok(m, int(f["x"]) + 100, tok, px)))
    for d in (1000, 5000, 30000):
        t = int(f["x"]) + d
        mu = mid_tok(m, t, tok) if end - t > 30000 else None
        r[f"mo{d // 1000}"] = None if mu is None else round((mu - px) / 100, 3)
    tx = f["x"] + 5000
    if tx < end - 2000:
        g = dsim.taker(m, int(tx + sc["hold"]), tok, 100, 5.0, sell=True)
        if g is not None:
            r["exit5_c"] = round((g["px"] - px) / 100 - fee_c - 100 * g["fee"] / g["size"], 3)
    if winner is not None:
        r["settle_c"] = round(100 * ((1.0 if (winner == "UP") == (tok == 0) else 0.0)) - px / 100 - fee_c, 3)
    out.append(r)
    return True


def run_market(m, sc, winner, out, W):
    B, P = m["book"], m["pr"]
    e = m["key"][2]
    t0, end = e * 1000, (e + W) * 1000
    x = B["x"].astype(np.int64)
    b, k = B["b"].astype(np.int64), B["k"].astype(np.int64)
    bs, ks = B["bs"].astype(float), B["ks"].astype(float)
    last = -10**15
    for t in range(t0 + 1000, end - 30000, 250):  # F5
        if t - last < 30000:
            continue
        i = int(np.searchsorted(x, t, "right")) - 1
        if i < 0 or b[i] == 0 or k[i] == 0 or k[i] - b[i] > 100 or bs[i] + ks[i] <= 0:
            continue
        im = (bs[i] - ks[i]) / (bs[i] + ks[i])
        if abs(im) >= 0.8 and trade(m, sc, "F5", t, 0 if im > 0 else 1, 1, end, winner, out):
            last = t
    last = -10**15
    for j in range(len(P["x"])):  # F1
        t = int(P["x_print"][j])
        if not t0 <= t < end - 30000 or t - last < 30000:
            continue
        up_px = int(P["up"][j])
        tok = 1 if P["hb"][j] else 0
        usd = float(P["sz"][j]) * ((up_px if tok == 0 else 10000 - up_px) / 1e4)
        if usd >= 50 and trade(m, sc, "F1", t, tok, 1, end, winner, out):
            last = t
    t = end - 60000  # F6
    i = int(np.searchsorted(x, t, "right")) - 1
    if i >= 0 and b[i] and k[i]:
        trade(m, sc, "F6", t, 0 if (b[i] + k[i]) / 2 >= 5000 else 1, 1, end, winner, out)
    for X in (6000, 7000, 8000):  # late breakout
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
    import csv as _csv
    day, asset, tf = sys.argv[1], sys.argv[2], int(sys.argv[3])
    dsim.FEE_RATE = 0.05 if asset == "btc" else 0.01
    sc = pmlat.SC["live"]
    OUT.mkdir(parents=True, exist_ok=True)
    W = {r["slug"]: r["winner"] for r in _csv.DictReader(open(Path.home() / "dublin-1124/settle.csv"))}
    out = []
    for h, mk in pmlat.hours(day, asset, tf, with_act=False):
        for e, m in mk.items():
            run_market(m, sc, W.get(pmlat.slug(m["key"])), out, 60 * tf)
    cols = ["rule", "e", "tok", "filled", "ask", "px", "tleft", "fee_c", "pess", "mo1", "mo5", "mo30", "exit5_c", "settle_c"]
    with gzip.open(OUT / f"taker-{asset}-{tf}-{day}.csv.gz", "wt") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(day, asset, tf, "wrote", len(out), flush=True)


if __name__ == "__main__":
    main()
