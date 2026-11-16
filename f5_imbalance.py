"""Family 5: top-of-book imbalance / last prints as a short-horizon predictor (BTC 5m, UP book).

At every book row r we know bid/ask/sizes. Signal I = (bs - ks) / (bs + ks). Target: UP mid change from the row we
see (x + book) to x + book + H for H in 100 ms, 1 s, 5 s (in c). Also: whether the next change of the best ask/bid
is up or down. Sampled at most once per 250 ms per market to keep rows independent-ish.
Taker test: when |I| >= thr and spread == 1 tick, BUY the favoured token at its ask (dsim taker) and exit at the
bid after H (taker) or settle. Usage: python f5_imbalance.py DAY SCEN -> out/pmlat/f5-{scen}-{day}.csv.gz
"""

import gzip
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001
import dsim

HS = (100, 1000, 5000)
THR = 0.8


def main():
    day, scn = sys.argv[1], sys.argv[2]
    sc = pmlat.SC[scn]
    out = []
    W = pmlat.winners([])
    for h, mk in pmlat.hours(day, "btc", 5, with_act=False):
        for e, m in mk.items():
            win = W.get(pmlat.slug(m["key"]))
            B = m["book"]
            x = B["x"].astype(np.int64)
            end = (e + 300) * 1000
            last = -1e18
            for r in range(1, len(x)):
                if x[r] - last < 250 or x[r] < e * 1000 or x[r] > end - 10000:
                    continue
                b, k, bs, ks = int(B["b"][r]), int(B["k"][r]), float(B["bs"][r]), float(B["ks"][r])
                if b == 0 or k == 0 or b < 500 or k > 9500:
                    continue
                last = x[r]
                imb = (bs - ks) / (bs + ks)
                td = int(x[r]) + sc["book"]
                md = (b + k) / 2
                rec = dict(e=e, x=int(x[r]), spr=k - b, imb=imb, bs=bs, ks=ks, mid=md)
                for H in HS:
                    mh = dsim.mid(m, td + H, 0)
                    rec[f"dm{H}"] = None if mh is None else (mh - md) / 100
                if abs(imb) >= THR and k - b == 100:
                    tok = 0 if imb > 0 else 1
                    ask = k if tok == 0 else 10000 - b
                    f = dsim.taker(m, td + sc["order"] + sc["hold"], tok, ask, 5.0)
                    if f is not None:
                        fin = f["fee"] / f["size"]
                        rec["tok"] = tok
                        rec["px"] = f["px"]
                        if win is not None:
                            rec["settle"] = 100 * ((1.0 if (win == "UP") == (tok == 0) else 0.0) - f["px"] / 1e4 - fin)
                        for H in (1000, 5000):
                            t2 = td + 2 * (sc["order"] + sc["hold"]) + sc["book"] + H
                            g = dsim.taker(m, t2, tok, 0, 5.0, sell=True) if t2 < end else None
                            rec[f"tk{H}"] = None if g is None else 100 * ((g["px"] - f["px"]) / 1e4 - fin - g["fee"] / g["size"])
                out.append(rec)
        print(day, scn, h, len(out), flush=True)
    f = pmlat.OUT / f"f5-{scn}-{day}.csv.gz"
    with gzip.open(f, "wt") as fo:
        pd.DataFrame(out).to_csv(fo, index=False)
    print("wrote", f, len(out))


if __name__ == "__main__":
    main()
