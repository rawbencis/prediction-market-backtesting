"""Family 6: the 60-s-left burst in BTC 5m (second 240 of the window), dsim fills, one trade per market per variant.

Favourite = the token with UP/DOWN mid >= 50c in the book seen at decision time.
(A) maker: at e + T_POST s post-only SELL of the favourite K ticks above its ask (= BUY the underdog at 1 - (ask + K)),
    5 shares, cancel decided at e + 240 + C s. Models pess / prop / front. Held to settlement (the underdog leg; a sold
    favourite = long underdog), plus mid markouts of the underdog at +1 s / +30 s after the fill.
(C) taker fade: watch prints in [240.0, 240.0 + Wd s) (seen at print + book); if favourite-buying prints total >= X $,
    decide at the first moment the threshold is crossed (or at 240 + Wd when X = 0), BUY the underdog FAK at its ask
    + 1 tick; exits settle and taker sell at +30 s.
(F) taker follow, for contrast: same trigger, BUY the favourite.
Usage: python f6_sim.py DAY SCEN -> out/pmlat/f6-{scen}-{day}.csv.gz
"""

import gzip
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001
import dsim

T_POSTS = (239.0, 239.7)
KS = (0, 100, 200)
CS = (0.5, 1.5)
MODELS = ("pess", "prop", "front")
XS = (0, 50, 200)
WDS = (0.25, 0.5, 1.0)


def fav_at(m, t):
    B, i = m["book"], dsim.state(m, t, strict=False)
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return None
    up_mid = (int(B["b"][i]) + int(B["k"][i])) / 2
    fav = 0 if up_mid >= 5000 else 1
    ask_fav = int(B["k"][i]) if fav == 0 else 10000 - int(B["b"][i])
    return fav, ask_fav, int(B["k"][i]) - int(B["b"][i])


def run_market(m, sc, win):
    e = m["key"][2]
    E = e * 1000
    out = []
    pay = lambda tok: None if win is None else (1.0 if (win == "UP") == (tok == 0) else 0.0)  # noqa: E731
    # (A) maker
    for tpost in T_POSTS:
        td = E + int(tpost * 1000)
        fa = fav_at(m, td)
        if fa is None:
            continue
        fav, ask, spr = fa
        und = 1 - fav
        for K in KS:
            q = ask + K  # favourite price we sell at
            if q >= 9900:
                continue
            for C in CS:
                t_cxl = E + int((240 + C) * 1000) + sc["cancel"]
                for model in MODELS:
                    res = dsim.maker(m, td + sc["order"], und, 10000 - q, 5.0, t_cxl, model=model)
                    filled = sum(z["size"] for z in res["fills"])
                    r = dict(e=e, kind="A", tpost=tpost, K=K, C=C, model=model, fav=fav, fav_ask=ask, spr=spr,
                             status=res["status"], filled=filled)
                    if filled > 0:
                        tf = res["fills"][0]["x"]
                        r["tfill"] = (tf - E) / 1000
                        p_und = 10000 - q
                        p = pay(und)
                        r["settle_c"] = None if p is None else 100 * (p - p_und / 1e4)
                        for H in (1000, 30000):
                            mm = dsim.mid(m, tf + H, und)
                            r[f"mo{H}"] = None if mm is None else (mm - p_und) / 100
                    out.append(r)
    # (C)/(F) taker after the burst
    P = m["pr"]
    seen = P["x_print"].astype(np.int64) + sc["book"]
    fa0 = fav_at(m, E + 239900)
    if fa0 is None:
        return out
    fav = fa0[0]
    favbuy = (P["hb"] == (fav == 1))  # UP favourite: taker lifted the UP ask (hb False) is a favourite buy
    usd = np.where(P["hb"], (10000 - P["up"]) / 1e4, P["up"] / 1e4) * P["sz"]
    for Wd in WDS:
        lo, hi = E + 240000, E + int((240 + Wd) * 1000)
        sel = (P["x_print"] >= lo) & (P["x_print"] < hi) & favbuy
        o = np.argsort(seen[sel])
        cum = np.cumsum(usd[sel][o])
        ts = seen[sel][o]
        for X in XS:
            if X == 0:
                td = hi + sc["book"]
            else:
                k = np.searchsorted(cum, X)
                if k >= len(cum):
                    continue
                td = int(ts[k])
            for kind, tok in (("C", 1 - fav), ("F", fav)):
                B, i = m["book"], dsim.state(m, td, strict=False)
                if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
                    continue
                ask = int(B["k"][i]) if tok == 0 else 10000 - int(B["b"][i])
                f = dsim.taker(m, td + sc["order"] + sc["hold"], tok, ask + 100, 5.0)
                r = dict(e=e, kind=kind, Wd=Wd, X=X, fav=fav, filled=0.0 if f is None else 5.0, tdec=(td - E) / 1000,
                         burst_usd=float(cum[-1]) if len(cum) else 0.0)
                if f is not None:
                    fin = f["fee"] / f["size"]
                    p = pay(tok)
                    r["px"] = f["px"]
                    r["settle_c"] = None if p is None else 100 * (p - f["px"] / 1e4 - fin)
                    t2 = td + sc["order"] + sc["hold"] + sc["book"] + 30000 + sc["order"] + sc["hold"]
                    g = dsim.taker(m, t2, tok, 0, 5.0, sell=True)
                    r["tk30000"] = r["settle_c"] if g is None else 100 * ((g["px"] - f["px"]) / 1e4 - fin - g["fee"] / g["size"])
                out.append(r)
    return out


def main():
    day, scn = sys.argv[1], sys.argv[2]
    sc = pmlat.SC[scn]
    out = []
    W = pmlat.winners([])
    for h, mk in pmlat.hours(day, "btc", 5, with_act=False):
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            out += run_market(m, sc, W.get(pmlat.slug(m["key"])))
        print(day, scn, h, len(out), flush=True)
    f = pmlat.OUT / f"f6-{scn}-{day}.csv.gz"
    with gzip.open(f, "wt") as fo:
        pd.DataFrame(out).to_csv(fo, index=False)
    print("wrote", f, len(out))


if __name__ == "__main__":
    main()
