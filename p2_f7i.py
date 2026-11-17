"""15m study, Part 2: frozen F7i-A / F7i-B (frozen/F7i.json; f3_maker improve + f7_altpull pull) on a 15m target.
Pull source: alt15 = the other four assets' books of the same timeframe (f7_altpull's default), or
             b5a5   = the concurrent BTC 5m book and the ETH/SOL/XRP/DOGE 5m books inside the 15m window.
A = rows with rules out+opp+dep+alt+ttl and own imbalance > 0.6 (applied after the run, as holdout_eval.py does); B = out+alt+ttl.
Usage: PMLAT_MAXDAY=... [PMLAT_STRICT=1] python p2_f7i.py DAY SCEN SRC [asset tf] -> out/p2/f7i15-{src}-{asset}{tf}-{scn}-{day}{-strict}.csv.gz
"""
import sys
from pathlib import Path

import f9_live  # noqa: F401
import f3_maker
import f7_altpull
import pmlat
from f3_maker import NW, OUTF, flush

OUT = Path(__file__).resolve().parent / "out" / "p2"
ALTS = ("eth", "sol", "xrp", "doge")


def main():
    day, scn, src = sys.argv[1], sys.argv[2], sys.argv[3]
    asset = sys.argv[4] if len(sys.argv) > 4 else "btc"
    tf = int(sys.argv[5]) if len(sys.argv) > 5 else 15
    f3_maker.MODE, f3_maker.ACKC, f3_maker.MODELS = "improve", True, ("pess",)
    sc = pmlat.SC[scn]
    rules = (("out", "alt", "ttl"), ("out", "opp", "dep", "alt", "ttl"))
    OUT.mkdir(parents=True, exist_ok=True)
    OUTF[0] = OUT / f"f7i15-{src}-{asset}{tf}-{scn}-{day}{'-strict' if f3_maker.STRICT else ''}.csv.gz"
    W = pmlat.winners([])
    others = [a for a in ("btc",) + ALTS if a != asset]
    gens = {}
    if src == "alt15":
        gens = {a: pmlat.hours(day, a, tf, with_act=False) for a in others}
    else:
        gens = {a: pmlat.hours(day, a, 5, with_act=False) for a in ("btc",) + ALTS}
    cur = {}

    def adv(key, gen, h):
        while cur.get(key, (None,))[0] is None or cur[key][0] < h:
            try:
                cur[key] = next(gen)
            except StopIteration:
                cur[key] = (99, {})
        return cur[key][1] if cur[key][0] == h else {}

    out = []
    for h, mk in pmlat.hours(day, asset, tf, with_act=False):
        books = {a: adv(a, g, h) for a, g in gens.items()}
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            if src == "alt15":
                trig = [books[a][e] for a in others if e in books[a]]
            else:
                trig = [mm for a in books for ee, mm in books[a].items() if e <= ee < e + tf * 60]
            f7_altpull.ALT.clear()
            f7_altpull.ALT.update(f7_altpull.alt_events(trig, 50))
            rows = f3_maker.run_market(m, sc, W.get(pmlat.slug(m["key"])), rules)
            for r in rows:
                r["thr"] = 50
                r["n_alts"] = len(trig)
            out += rows
        flush(out)
        print(day, scn, src, h, NW[0], flush=True)
    flush(None)
    print("wrote", OUTF[0], NW[0])


if __name__ == "__main__":
    main()
