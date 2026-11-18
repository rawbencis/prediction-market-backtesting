"""15m ladder follow-up, step 2: post-only BUY 5 at k ticks below the best bid, BTC 15m, both tokens, on the feeds-rec
5-level book (pmbook: UP book, DOWN = mirror; trade prints with tx on the same socket). Polymarket data only.

Rule per token (one order per token at a time):
  send   in a calm moment (UP best bid and ask unchanged over the previous 1 s; seen +9 ms), price q = token best bid -
         k ticks (1 tick = 1c), if q >= 1c, q's band and the minutes left pass the cell's filters, >= 30 s left, and the
         lead cap holds: (this token filled + resting + 5) - (other token filled + resting) <= 10. On book +55 ms.
  queue  pess: behind all size displayed at q when we land (q below the 5 shown levels: unknown until shown, then
         behind what is shown then); prop: as pess, plus cancels at q (size drops not explained by prints at q)
         advance us pro rata (dsim prop).
  fills  strict: a print hitting the token's bid at q fills (size - queue ahead consumed); a print through q (at a
         lower price) fills at most its own size minus the queue ahead (our order is a phantom).
  rest   until filled, until the best bid is more than 6 ticks above q (seen +9, cancel lands +90), or 30 s before the
         close (cancel lands +90). Then the next calm moment re-posts by the same rule. Hold to settlement.
Cells: k 1..4 x band (all | ext: q < 15c or q > 85c | mid: 15-85c) x minutes left at send (all | 10: last 10 | 5:
last 5) x queue (pess | prop).
Usage: python ladder.py DAY [cells|all] -> out/ladder/lad-{day}.csv.gz (fills) + -orders.csv + -mk.csv
"""
import csv
import gzip
import itertools
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import orjson

import pmlat
from f8_grid import next_stable
from mk_level import FR, LAST_HOUR, Mk, bad_window, hours_for, lines15, tok_book

OUT = Path(__file__).resolve().parent / "out" / "ladder"
SIZE, LEAD, MAXDIST, BOOK, POST, CXL, STOP = 5.0, 10.0, 600, 9, 55, 90, 30000
# pmbook prints carry a timestamp ~17 ms (median) after the book row that shows the trade (step 0). Prints are moved
# this much earlier so that a print is processed before its own level drop (else the queue ahead is cut twice).
PRINT_SHIFT = int(os.environ.get("LAD_SHIFT", "60"))
CELLS = ["|".join(c) for c in itertools.product(("1", "2", "3", "4"), ("all", "ext", "mid"), ("all", "10", "5"), ("pess", "prop"))]
COLS = ["cell", "e", "tok", "q", "t_send", "t_arr", "t_cxl", "ahead0", "filled", "tfill", "tleft", "mo1000", "mo5000",
        "mo30000", "settle_c", "rebate_c"]


def band_ok(q, band):
    return band == "all" or ((q < 1500 or q > 8500) if band == "ext" else 1500 <= q <= 8500)


class Book:
    def __init__(self, mk, e):
        self.e, self.end = e, (e + 900) * 1000
        self.x, L, self.drop = mk.arrays()
        self.tb = {t: tok_book(L, t) for t in (0, 1)}
        b0, a0 = self.tb[0][0][:, 0], self.tb[0][2][:, 0]
        ch = np.r_[True, (b0[1:] != b0[:-1]) | (a0[1:] != a0[:-1])]
        self.C = self.x[ch]
        # prints hitting each token's bid, in that token's price (1e-4), deduplicated by (tx, price)
        self.hits = {}
        T = sorted(mk.tr)
        for tok in (0, 1):
            seen, hx, hp, hs = set(), [], [], []
            for t in T:
                if t[1] == tok and t[2] == 1:
                    p = round(t[3] * 1e4)
                elif t[1] != tok and t[2] == 0:
                    p = 10000 - round(t[3] * 1e4)
                else:
                    continue
                key = (t[5], p)
                if key in seen:
                    continue
                seen.add(key)
                hx.append(t[0] - PRINT_SHIFT), hp.append(p), hs.append(t[4])
            self.hits[tok] = (np.array(hx, np.int64), np.array(hp, np.int64), np.array(hs, float))

    def row(self, t):
        return int(np.searchsorted(self.x, t, "right")) - 1

    def mid(self, t, tok):
        j = self.row(t)
        if j < 0 or self.drop[j]:
            return None
        BP, _, AP, _ = self.tb[tok]
        if BP[j, 0] <= 0 or AP[j, 0] <= BP[j, 0]:
            return None
        return (int(BP[j, 0]) + int(AP[j, 0])) / 2

    def order(self, tok, q, t_arr, t_stop, model):
        """-> (t_cxl or fill-complete time, fills [(x, size)], ahead0)."""
        x, drop = self.x, self.drop
        BP, BS, _, _ = self.tb[tok]
        i0 = self.row(t_arr)
        # cancel: first row seen after arrival with best bid > q + MAXDIST, else the stop time
        i1 = int(np.searchsorted(x, t_stop - BOOK, "left"))
        far = np.flatnonzero((BP[i0 + 1:i1, 0] > q + MAXDIST) & ~drop[i0 + 1:i1]) if i1 > i0 + 1 else np.zeros(0, int)
        t_cxl = (int(x[i0 + 1 + far[0]]) + BOOK + CXL) if len(far) else t_stop + CXL
        t_cxl = max(t_cxl, t_arr + CXL)
        # queue at landing
        def shown(i):
            row = BP[i]
            k = np.flatnonzero(row == q)
            if len(k):
                return float(BS[i, k[0]])
            full = (row > 0).sum() == 5
            if full and q < row[row > 0].min():
                return None  # hidden below the 5 shown levels
            return 0.0

        ahead = shown(i0) if i0 >= 0 else 0.0
        ahead0 = ahead
        hx, hp, hs = self.hits[tok]
        j0, j1 = np.searchsorted(hx, t_arr, "right"), np.searchsorted(hx, t_cxl, "right")
        pr = [(int(hx[j]), int(hp[j]), float(hs[j])) for j in range(j0, j1) if hp[j] <= q]
        # rows where the size shown at q changes (for prop and for hidden -> shown)
        ia, ib = i0 + 1, int(np.searchsorted(x, t_cxl, "left"))
        ev = []
        if ib > ia:
            seg = BP[ia:ib]
            sq = np.where(seg == q, BS[ia:ib], 0.0).sum(1)
            vis = (seg == q).any(1)
            full = (seg > 0).sum(1) == 5
            lastp = np.where(seg > 0, seg, 10**9).min(1)
            hid = full & (q < lastp) & ~vis
            prev_sq = np.r_[ahead0 if ahead0 is not None else -1.0, sq[:-1]]
            prev_hid = np.r_[ahead0 is None, hid[:-1]]
            chg = np.flatnonzero(((sq != prev_sq) | (hid != prev_hid)) & ~drop[ia:ib])
            ev = [(int(x[ia + c]), -1, float(sq[c]), bool(hid[c])) for c in chg]
        events = sorted([(t, 0, p, n) for t, p, n in pr] + [(t, 1, s, h) for t, _, s, h in ev])
        left, fills, disp, since = SIZE, [], ahead0, 0.0
        for t, kind, a, b in events:
            if left <= 1e-9:
                break
            if kind == 0:  # print hitting our side at price a (<= q), size b
                if a == q:
                    since += b
                a0 = ahead if ahead is not None else 0.0
                d = min(a0, b)
                if ahead is not None:
                    ahead -= d
                n = min(left, b - d)
                if n > 1e-9:
                    left -= n
                    fills.append((t, n))
            else:  # shown size at q is now a (hidden: b)
                if b:
                    disp, since = None, 0.0
                    continue
                if ahead is None:
                    ahead = a
                elif model == "prop" and disp is not None:
                    cxl = disp - a - since
                    D = disp - since
                    if cxl > 0 and D > 0:
                        ahead = max(0.0, ahead - cxl * ahead / D)
                ahead = min(ahead, a)
                disp, since = a, 0.0
        t_done = fills[-1][0] if left <= 1e-9 else t_cxl
        return t_done, fills, ahead0


def run_cell(B, cell, win, out, cnt):
    k, band, mins, model = cell.split("|")
    k = int(k)
    t_free = [B.e * 1000, B.e * 1000]
    orders = {0: [], 1: []}

    def known(tok, t):
        f = r = 0.0
        for o in orders[tok]:
            ff = sum(n for tt, n in o["fills"] if tt <= t)
            f += ff
            if o["t_send"] <= t < o["t_done"]:
                r += SIZE - ff
        return f, r

    t_last = B.end - STOP
    while True:
        cand = []
        for tok in (0, 1):
            if t_free[tok] is None:
                continue
            t = next_stable(B.C, t_free[tok]) + BOOK
            if t > t_last - POST:
                t_free[tok] = None
                continue
            cand.append((t, tok))
        if not cand:
            break
        t_dec, tok = min(cand)
        i = B.row(t_dec - BOOK)
        nxt = int(np.searchsorted(B.x, t_dec - BOOK, "right"))
        step = int(B.x[nxt]) if nxt < len(B.x) else None
        BP = B.tb[tok][0]
        left_ms = B.end - t_dec
        ok = i >= 0 and not B.drop[i] and BP[i, 0] > 0 and B.tb[tok][2][i, 0] > 0
        q = int(BP[i, 0]) - 100 * k if ok else 0
        if mins != "all" and left_ms > int(mins) * 60000:
            t_free[tok] = B.end - int(mins) * 60000 - BOOK  # nothing to do until the window's last minutes
            continue
        if not ok or q < 100 or not band_ok(q, band):
            t_free[tok] = step  # next book change
            if step is None:
                t_free[tok] = None
            continue
        fm, rm = known(tok, t_dec)
        fo, ro = known(1 - tok, t_dec)
        if (fm + rm + SIZE) - (fo + ro) > LEAD + 1e-9:
            # wait for the other side to fill; re-check at the next book change
            t_free[tok] = step
            if step is None:
                t_free[tok] = None
            continue
        t_arr = t_dec + POST
        t_done, fills, a0 = B.order(tok, q, t_arr, t_last, model)
        o = dict(t_send=t_dec, t_done=t_done, fills=fills)
        orders[tok].append(o)
        t_free[tok] = max(t_done, t_dec + 1)
        c = cnt.setdefault(cell, [0, 0])
        c[0] += 1
        if fills:
            c[1] += 1
            tf_ = fills[0][0]
            r = dict(cell=cell, e=B.e, tok=tok, q=q, t_send=t_dec, t_arr=t_arr, t_cxl=t_done, ahead0=a0,
                     filled=sum(n for _, n in fills), tfill=tf_, tleft=B.end - tf_,
                     rebate_c=round(100 * 0.014 * (q / 1e4) * (1 - q / 1e4), 4))
            for h in (1000, 5000, 30000):
                mm = B.mid(tf_ + h, tok) if tf_ + h < B.end - STOP else None
                r[f"mo{h}"] = None if mm is None else (mm - q) / 100
            if win is not None:
                r["settle_c"] = 100 * ((1.0 if (win == "UP") == (tok == 0) else 0.0) - q / 1e4)
            out.append(r)


def main():
    day = sys.argv[1]
    cells = CELLS if len(sys.argv) < 3 or sys.argv[2] == "all" else sys.argv[2].split(";")
    tag = os.environ.get("LAD_TAG", "")
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"lad-{day}{tag}"
    fo = gzip.open(f"{base}.csv.gz", "wt")
    w = csv.DictWriter(fo, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    mko = open(f"{base}-mk.csv", "w")
    mko.write("e,winner,rows,bad\n")
    W = pmlat.winners([])
    d0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    last_e = d0 + 3600 * (LAST_HOUR.get(day, 23) + 1) - 900
    cnt, open_ = {}, {}

    def fin(e):
        mk = open_.pop(e)
        sl = pmlat.slug(("btc", 15, e))
        if sl not in W:
            W.update(pmlat.winners([sl]))
        win = W.get(sl)
        B = Book(mk, e)
        mko.write(f"{e},{win or ''},{len(B.x)},{int(bad_window(e))}\n")
        if len(B.x) < 100 or bad_window(e):
            return
        out = []
        for cell in cells:
            run_cell(B, cell, win, out, cnt)
        w.writerows(out)
        fo.flush()
        mko.flush()

    for dd, h in hours_for(day):
        p = FR / dd / f"pmbook-{h:02d}.jsonl.gz"
        if p.exists():
            for line in lines15(p):
                if True:
                    dct = orjson.loads(line)
                    e = int(dct["slug"].rsplit("-", 1)[1])
                    if not d0 <= e <= last_e:
                        continue
                    open_.setdefault(e, Mk()).add(dct)
        hend = int(datetime.fromisoformat(dd).replace(tzinfo=timezone.utc).timestamp()) + 3600 * (h + 1)
        for e in [e for e in open_ if e + 900 + 3600 <= hend]:
            fin(e)
        print(day, dd, h, flush=True)
    for e in list(open_):
        fin(e)
    fo.close()
    mko.close()
    with open(f"{base}-orders.csv", "w") as f:
        f.write("cell,orders,filled_orders\n")
        for c, v in cnt.items():
            f.write(f"{c},{v[0]},{v[1]}\n")
    print("wrote", base, flush=True)


if __name__ == "__main__":
    main()
