"""F9 item 1: frozen F7i-B (frozen/F7i.json: F3i improve entry, pulls out+alt+ttl, TTL 5 s, alt THR 50, pess, ackc) rerun
under a latency scenario, recording for every order whether a pull signal was seen before the order landed.
Strategy logic is F7i's own code (f7_altpull + f3_maker.run_market); this file only restricts it to B / pess and wraps
pull_time to keep the raw signal time. Usage: PMLAT_MAXDAY=2026-11-16 python f9_calib.py DAY SCEN [hours|all]
-> out/pmlat/f9calib-{scen}-{day}.csv.gz (one row per order)
"""
import sys

import f9_live  # noqa: F401  (registers live / live_q)
import f3_maker
import f7_altpull
import pmlat
from f3_maker import NW, OUTF, flush

RAW = []
_pt = f3_maker.pull_time  # f7_altpull's version (book rules + alt)


def pull_time(m, r0, side, q, sz0, t_arr, ttl, sc, rules):
    t = _pt(m, r0, side, q, sz0, t_arr, ttl, sc, rules)
    RAW.append(t - sc["cancel"])  # time the pull signal was seen (TTL expiry counts as a signal at t_arr + ttl)
    return t


def main():
    day, scn = sys.argv[1], sys.argv[2]
    hrs = range(24) if len(sys.argv) <= 3 or sys.argv[3] == "all" else [int(h) for h in sys.argv[3].split(",")]
    f3_maker.MODE, f3_maker.ACKC, f3_maker.MODELS = "improve", True, ("pess",)
    f3_maker.pull_time = pull_time
    sc = pmlat.SC[scn]
    rules = (("out", "alt", "ttl"),)
    OUTF[0] = pmlat.OUT / f"f9calib-{scn}-{day}.csv.gz"
    W = pmlat.winners([])
    alts = f7_altpull.ALTS
    gens = {a: pmlat.hours(day, a, 5, hrs=hrs, with_act=False) for a in alts}
    cur = {a: (None, {}) for a in alts}
    out = []
    for h, mk in pmlat.hours(day, "btc", 5, hrs=hrs, with_act=False):
        for a in alts:
            while cur[a][0] is None or cur[a][0] < h:
                try:
                    cur[a] = next(gens[a])
                except StopIteration:
                    cur[a] = (99, {})
        miss = [pmlat.slug(m["key"]) for m in mk.values() if pmlat.slug(m["key"]) not in W]
        if miss:
            W.update(pmlat.winners(miss))
        for e, m in mk.items():
            altm = [cur[a][1][e] for a in alts if cur[a][0] == h and e in cur[a][1]]
            f7_altpull.ALT.clear()
            f7_altpull.ALT.update(f7_altpull.alt_events(altm, 50))
            RAW.clear()
            rows = f3_maker.run_market(m, sc, W.get(pmlat.slug(m["key"])), rules)
            assert len(rows) == len(RAW), (len(rows), len(RAW))
            for r, s in zip(rows, RAW):
                t_arr = r["x0"] + sc["book"] + r["olat"]
                r["sig_rel"] = s - t_arr  # < 0: the pull signal came before the order was on the book
                r["thr"] = 50
            out += rows
        flush(out)
        print(day, scn, h, NW[0], flush=True)
    flush(None)
    print("wrote", OUTF[0], NW[0])


if __name__ == "__main__":
    main()
