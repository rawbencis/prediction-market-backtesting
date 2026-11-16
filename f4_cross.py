"""Family 4: stale quotes on the sibling book after a BTC 5m print/sweep.

In Polymarket's unified book UP and DOWN of one market are the same book (DOWN ask = 1 - UP bid), so the
"complementary token" can't be stale on its own; the stale quotes that can exist are in sibling markets.
Event: a BTC 5m print with usd >= MIN_USD (event table). Sibling = the BTC 15m window that contains it.
Per event: 15m mid move in the print direction at +100 ms / 1 s / 5 s / 30 s after we see it, and a taker:
BUY the direction token on the 15m book FAK at its ask seen + 1 tick (5 shares, decide at print + book delay),
exits: settle, taker sell at +5 s / +30 s.
Usage: python f4_cross.py DAY SCEN -> out/pmlat/f4-{scen}-{day}.csv.gz
"""

import gzip
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001
import dsim

MIN_USD = 50.0


def main():
    day, scn = sys.argv[1], sys.argv[2]
    sc = pmlat.SC[scn]
    ev = pd.read_csv(pmlat.OUT / f"ev-btc5-{day}.csv.gz", usecols=["e", "j", "tp", "dir", "usd", "swept", "tleft"])
    ev = ev[ev.usd >= MIN_USD].sort_values("tp")
    out = []
    W = pmlat.winners([])
    for h, mk in pmlat.hours(day, "btc", 15, with_act=False):
        sl = [pmlat.slug(m["key"]) for m in mk.values()]
        W.update(pmlat.winners([s for s in sl if s not in W]))
        for e15, m in mk.items():
            end = (e15 + 900) * 1000
            sub = ev[(ev.tp >= e15 * 1000) & (ev.tp < end - 2000)]
            win = W.get(pmlat.slug(m["key"]))
            for r in sub.itertuples():
                td = int(r.tp) + sc["book"]
                md = dsim.mid(m, td, 0)
                rec = dict(e5=r.e, j=r.j, e15=e15, usd=r.usd, swept=r.swept, dir=r.dir, tleft15=end - td)
                for hh in (100, 1000, 5000, 30000):
                    mh = dsim.mid(m, td + hh, 0) if td + hh < end else None
                    rec[f"mv{hh}"] = None if (md is None or mh is None) else r.dir * (mh - md) / 100
                tok = 0 if r.dir == 1 else 1
                B, i = m["book"], dsim.state(m, td, strict=False)
                if i >= 0 and B["k"][i] > 0 and B["b"][i] > 0:
                    ask = int(B["k"][i]) if tok == 0 else 10000 - int(B["b"][i])
                    f = dsim.taker(m, td + sc["order"] + sc["hold"], tok, ask + 100, 5.0)
                    if f is not None:
                        px, fin = f["px"], f["fee"] / f["size"]
                        rec["px"] = px
                        if win is not None:
                            rec["settle"] = 100 * ((1.0 if (win == "UP") == (tok == 0) else 0.0) - px / 1e4 - fin)
                        for H in (5000, 30000):
                            t2 = td + sc["order"] + sc["hold"] + sc["book"] + H + sc["order"] + sc["hold"]
                            g = dsim.taker(m, t2, tok, 0, 5.0, sell=True) if t2 < end else None
                            rec[f"tk{H}"] = None if g is None else 100 * ((g["px"] - px) / 1e4 - fin - g["fee"] / g["size"])
                out.append(rec)
        print(day, scn, h, len(out), flush=True)
    f = pmlat.OUT / f"f4-{scn}-{day}.csv.gz"
    with gzip.open(f, "wt") as fo:
        pd.DataFrame(out).to_csv(fo, index=False)
    print("wrote", f, len(out))


if __name__ == "__main__":
    main()
