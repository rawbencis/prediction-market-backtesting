"""Family 7 (own idea): F3 maker on BTC 5m, protected by alt-book pulls.

BTC books reprice 230-300 ms after an outside move; ETH/SOL/XRP/DOGE books 70-100 ms (speed atlas 2026-11-14). The alt
books are Polymarket data, so a fast bot can pull a BTC quote when the alt books move against it, ~150 ms before the BTC
snipers arrive. Our cancel lands at alt row + book (9) + cancel (21) ms; cancels are not held, takers are held 50 ms.

Alt signal: for each alt 5m market of the same epoch, at every book row, dm = UP mid now - UP mid 500 ms earlier.
An adverse alt event for our BTC BUY UP (bid) is any alt with dm <= -THR; for our BTC offer (BUY DOWN) dm >= +THR.
Rules: F3's out/opp/dep/ttl plus 'alt'. Everything else as f3_maker (join mode, one live order per side, TTL 5 s).
Usage: python f7_altpull.py DAY SCEN [join|improve] -> out/pmlat/f7-{scen}-{day}.csv.gz
"""

import gzip
import sys

import numpy as np
import pandas as pd

import pmlat  # noqa: I001
import dsim
import f3_maker

ALTS = ("eth", "sol", "xrp", "doge")
THRS = (50, 100)
LOOK = 500
RULES = (("out", "ttl"), ("out", "alt", "ttl"), ("out", "opp", "dep", "alt", "ttl"))
ALT = {}  # side -> sorted adverse event times (set per market before run_market)


def alt_events(alt_mk, thr):
    dn, up = [], []
    for m in alt_mk:
        B = m["book"]
        ok = (B["b"] > 0) & (B["k"] > 0)
        x = B["x"][ok].astype(np.int64)
        mid = (B["b"][ok].astype(np.int64) + B["k"][ok].astype(np.int64)) / 2
        if len(x) < 2:
            continue
        j = np.searchsorted(x, x - LOOK, "right") - 1
        prev = np.where(j >= 0, mid[np.clip(j, 0, None)], np.nan)
        dm = mid - prev
        dn.append(x[dm <= -thr])
        up.append(x[dm >= thr])
    cat = lambda z: np.sort(np.concatenate(z)) if z else np.zeros(0, np.int64)  # noqa: E731
    return {0: cat(dn), 1: cat(up)}


_orig_pull = f3_maker.pull_time


def pull_time(m, r0, side, q, sz0, t_arr, ttl, sc, rules):
    t = _orig_pull(m, r0, side, q, sz0, t_arr, ttl, sc, tuple(r for r in rules if r != "alt"))
    if "alt" in rules:
        t_dec = int(m["book"]["x"][r0]) + sc["book"]
        a = ALT[side]
        k = np.searchsorted(a, t_dec, "right")
        if k < len(a):
            t = min(t, int(a[k]) + sc["book"] + sc["cancel"])
    return t


f3_maker.pull_time = pull_time


def main():
    day, scn = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        f3_maker.MODE = sys.argv[3]
    sc = pmlat.SC[scn]
    out = []
    W = pmlat.winners([])
    gens = {a: pmlat.hours(day, a, 5, with_act=False) for a in ALTS}
    alt_cur = {a: (None, {}) for a in ALTS}
    for h, mk in pmlat.hours(day, "btc", 5, with_act=False):
        for a in ALTS:  # advance each alt generator to hour h
            while alt_cur[a][0] is None or alt_cur[a][0] < h:
                try:
                    alt_cur[a] = next(gens[a])
                except StopIteration:
                    alt_cur[a] = (99, {})
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            alts = [alt_cur[a][1][e] for a in ALTS if alt_cur[a][0] == h and e in alt_cur[a][1]]
            for thr in THRS:
                ALT.clear()
                ALT.update(alt_events(alts, thr))
                rows = f3_maker.run_market(m, sc, W.get(pmlat.slug(m["key"])),
                                           RULES if thr == THRS[0] else tuple(r for r in RULES if "alt" in r))
                for r in rows:
                    r["thr"] = thr
                    r["n_alts"] = len(alts)
                out += rows
        print(day, scn, h, len(out), flush=True)
    f = pmlat.OUT / f"f7{'i' if f3_maker.MODE == 'improve' else ''}-{scn}-{day}.csv.gz"
    with gzip.open(f, "wt") as fo:
        pd.DataFrame(out).to_csv(fo, index=False)
    print("wrote", f, len(out))


if __name__ == "__main__":
    main()
