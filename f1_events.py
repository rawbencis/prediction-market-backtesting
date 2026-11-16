"""Family 1/2/5 event table: one row per trade print (BTC 5m by default), with the follow-on flow and the mid path.

Times in exchange ms. t_m = estimated match time (dsim), t_p = print stamp. We see the print at t_p + 9 ms (dublin).
dir = +1 if the taker bought UP / sold DOWN (lifted the UP ask), -1 if the taker hit the UP bid.
Follow-on windows are measured from t_p (when the public print exists): (0,50], (50,250], (250,1000], (1000,5000] ms.
Usage: python f1_events.py DAY [asset tf]  -> out/pmlat/ev-{asset}{tf}-{DAY}.csv.gz
"""

import gzip
import sys

import numpy as np

import pmlat  # noqa: I001  (sets sys.path for dsim)
import dsim

WINS = ((0, 50), (50, 250), (250, 1000), (1000, 5000))
MO = (100, 1000, 5000, 30000)


def rows_for_market(m):
    P, B = m["pr"], m["book"]
    n = len(P["x"])
    if n == 0:
        return []
    tm, tp = P["x"].astype(np.int64), P["x_print"].astype(np.int64)
    dirn = np.where(P["hb"], -1, 1)
    op = np.argsort(tp, kind="stable")
    tps, dps = tp[op], dirn[op]
    tw = m["twallet"]
    e = m["key"][2]
    out = []
    bx = B["x"]
    for j in range(n):
        px_tok = P["up"][j] if dirn[j] == 1 else 10000 - P["up"][j]
        r = dict(e=e, j=j, tm=int(tm[j]), tp=int(tp[j]), matched=int(P["matched"][j]), dir=int(dirn[j]),
                 up=int(P["up"][j]), px=int(px_tok), sz=float(P["sz"][j]), usd=float(P["sz"][j] * px_tok / 1e4),
                 tleft=(e + 300) * 1000 - int(tp[j]), w=(tw[j] if tw is not None else ""))
        # book just before the match and right after (dsim state)
        i0 = dsim.state(m, int(tm[j]))
        i1 = dsim.state(m, int(tm[j]), strict=False)
        if i0 < 0 or B["k"][i0] == 0:
            continue
        r.update(b0=int(B["b"][i0]), k0=int(B["k"][i0]), bs0=float(B["bs"][i0]), ks0=float(B["ks"][i0]))
        r.update(b1=int(B["b"][i1]), k1=int(B["k"][i1]), bs1=float(B["bs"][i1]), ks1=float(B["ks"][i1]))
        # did the print take the whole top level on its side?
        r["swept"] = int((B["k"][i1] > B["k"][i0]) if dirn[j] == 1 else (B["b"][i1] < B["b"][i0]))
        # follow-on prints after the print stamp
        for a, b in WINS:
            lo, hi = np.searchsorted(tps, tp[j] + a, "right"), np.searchsorted(tps, tp[j] + b, "right")
            seg = dps[lo:hi]
            r[f"same{b}"] = int((seg == dirn[j]).sum())
            r[f"opp{b}"] = int((seg == -dirn[j]).sum())
        # same-direction prints in the 5 s before (flow already running)
        lo = np.searchsorted(tps, tp[j] - 5000, "left")
        hi = np.searchsorted(tps, tp[j], "left")
        r["same_prev5s"] = int((dps[lo:hi] == dirn[j]).sum())
        # mid path in the print direction (1e-4 UP), from the mid before the match
        mb = dsim.mid(m, int(tm[j]) - 1)
        md = dsim.mid(m, int(tp[j]) + 9)
        r["mid_before"] = mb
        r["mid_seen"] = md
        for h in MO:
            mh = dsim.mid(m, int(tp[j]) + 9 + h)
            r[f"mv{h}"] = None if (mh is None or md is None or int(tp[j]) + 9 + h > (e + 300) * 1000) else dirn[j] * (mh - md)
        r["nrows_book"] = int(np.searchsorted(bx, tp[j] + 1000) - np.searchsorted(bx, tp[j]))
        out.append(r)
    return out


def main():
    day = sys.argv[1]
    asset = sys.argv[2] if len(sys.argv) > 2 else "btc"
    tf = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    pmlat.OUT.mkdir(parents=True, exist_ok=True)
    f = pmlat.OUT / f"ev-{asset}{tf}-{day}.csv.gz"
    cols = None
    nr = 0
    with gzip.open(f, "wt") as fo:
        for h, mk in pmlat.hours(day, asset, tf):
            for e, m in mk.items():
                for r in rows_for_market(m):
                    if cols is None:
                        cols = list(r)
                        fo.write(",".join(cols) + "\n")
                    fo.write(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n")
                    nr += 1
            print(day, h, nr, flush=True)
    print("wrote", f, nr)


if __name__ == "__main__":
    main()
