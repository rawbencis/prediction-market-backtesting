"""15m study, Part 1: how fast is the BTC 15m book (BTC 5m beside it). Streams one hour at a time (pmlat.hours).
Per window (target = BTC 15m or BTC 5m):
  book    : 1-s samples of spread (ticks) and size at the best levels, by minutes left
  prints  : prints per minute and their size / $ by minutes left
  levels  : new best level (f3 events): lifetime; time until the best bid is 1 / 2 ticks past it (same side)
  imb     : own-side top-of-book imbalance crossing >= 0.6: time until it falls below 0
  load    : book rows per second (all seconds / busiest), receive delay l - x overall and in the busiest seconds
  signals : (BTC 15m only) pull-signal rates against a resting UP bid: alt 15m / alt 5m books moving >= 0.5c / 1c within
            500 ms on >= 1/2/3 alts, BTC 5m book moving; survival of a resting order (no signal) for 15 / 60 / 180 s
  leads   : mid-move events (>= 0.5c in 500 ms): which book moves first, BTC 5m vs BTC 15m, alts vs BTC
Usage: PMLAT_MAXDAY=2026-11-16 python p1_15m.py DAY -> out/p1/p1-{day}.pkl (aggregated lists)
"""
import pickle
import sys
from pathlib import Path

import numpy as np

import pmlat
import f3_maker
from f9_grid import alt_per_alt, triggers, union

OUT = Path(__file__).resolve().parent / "out" / "p1"
ALTS = ("eth", "sol", "xrp", "doge")
MLB = {15: [0, 1, 3, 5, 10, 15.01], 5: [0, 1, 3, 5.01]}


def mleft_bucket(tf, sec_left):
    edges = MLB[tf]
    return int(np.searchsorted(edges, sec_left / 60, "right")) - 1


def anatomy(m, tf, agg):
    B = m["book"]
    x = B["x"].astype(np.int64)
    b, k = B["b"].astype(np.int64), B["k"].astype(np.int64)
    bs, ks = B["bs"].astype(float), B["ks"].astype(float)
    e = m["key"][2]
    t0, t1 = e * 1000, (e + tf * 60) * 1000
    # 1-s samples
    ts = np.arange(t0 + 1000, t1, 1000)
    i = np.searchsorted(x, ts, "right") - 1
    ok = (i >= 0) & (b[np.clip(i, 0, None)] > 0) & (k[np.clip(i, 0, None)] > 0)
    ii = i[ok]
    left = (t1 - ts[ok]) / 1000
    for lb, sp, s1, s2 in zip((mleft_bucket(tf, v) for v in left), (k[ii] - b[ii]) / 100, bs[ii], ks[ii]):
        agg.setdefault(("spread", tf, lb), []).append(float(sp))  # cents (15m can trade on a 0.1c tick)
        agg.setdefault(("bestsize", tf, lb), []).append(float(min(s1, s2)))
    # prints
    P = m["pr"]
    w = (P["x"] >= t0) & (P["x"] < t1)
    for xx, sz in zip(P["x"][w], P["sz"][w]):
        lb = mleft_bucket(tf, (t1 - xx) / 1000)
        agg.setdefault(("prints_n", tf, lb), [0])[0] += 1
        agg.setdefault(("print_sz", tf, lb), []).append(float(sz))
    for lb in range(len(MLB[tf]) - 1):
        lo, hi = MLB[tf][lb], min(MLB[tf][lb + 1], tf)
        agg.setdefault(("minutes", tf, lb), [0.0])[0] += hi - lo
    # new best levels: lifetime, time until 1 / 2 ticks past (bid side; ask side mirrored)
    ups, dns = f3_maker.events(m)
    for side, rows in ((0, ups), (1, dns)):
        for r in rows:
            if not t0 <= x[r] < t1 - 20000:
                continue
            q = int(b[r] if side == 0 else k[r])
            later = slice(r + 1, len(x))
            cur = b[later] if side == 0 else k[later]
            gone = np.flatnonzero(cur != q)
            agg.setdefault(("lvl_life", tf), []).append(float(x[r + 1 + gone[0]] - x[r]) if len(gone) else np.nan)
            for n in (1, 2):
                past = np.flatnonzero((cur >= q + 100 * n) if side == 0 else ((cur <= q - 100 * n) & (cur > 0)))
                agg.setdefault((f"past{n}", tf), []).append(float(x[r + 1 + past[0]] - x[r]) if len(past) and x[r + 1 + past[0]] < t1 else np.nan)
    # imbalance >= 0.6 -> < 0
    tot = bs + ks
    im = np.where(tot > 0, (bs - ks) / np.where(tot > 0, tot, 1), 0.0)
    for own in (im, -im):
        hi_ = own >= 0.6
        cross = np.flatnonzero(hi_[1:] & ~hi_[:-1]) + 1
        for r in cross:
            if not t0 <= x[r] < t1 - 20000:
                continue
            neg = np.flatnonzero(own[r + 1:] < 0)
            agg.setdefault(("imb_decay", tf), []).append(float(x[r + 1 + neg[0]] - x[r]) if len(neg) else np.nan)
    # load
    w = (x >= t0 - 45000) & (x < t1)
    sec = x[w] // 1000
    if len(sec):
        u, c = np.unique(sec, return_counts=True)
        agg.setdefault(("rows_per_s", tf), []).extend(c.tolist())
        if "l" in B:
            dl = B["l"][w] / 1000 - x[w]
            agg.setdefault(("rx_delay", tf), []).extend(np.round(dl[:: max(1, len(dl) // 400)], 1).tolist())
            busy = u[c >= max(20, np.percentile(c, 99))]
            sel = np.isin(sec, busy)
            agg.setdefault(("rx_delay_busy", tf), []).extend(np.round(dl[sel], 1).tolist())
            agg.setdefault(("busy_rows", tf), []).extend(c[c >= np.percentile(c, 99)].tolist())


def signals(m15, alt15, alt5, btc5, agg):
    e = m15["key"][2]
    t0, t1 = e * 1000, (e + 900) * 1000
    mins = 15.0
    srcs = {}
    for thr in (50, 100):
        pa15, pa5 = alt_per_alt(alt15, thr), alt_per_alt(alt5, thr)
        for need in (1, 2, 3):
            srcs[f"alt15|{thr}|{need}"] = triggers(pa15, need, 1)  # against a resting UP bid: alts fall
            srcs[f"alt5|{thr}|{need}"] = triggers(pa5, need, 1)
        pb = alt_per_alt(btc5, thr)
        srcs[f"btc5|{thr}|1"] = union(pb, 1) if pb else np.zeros(0, np.int64)
    probe = np.arange(t0 + 5000, t1 - 30000, 5000)
    for name, ev in srcs.items():
        ev = ev[(ev >= t0) & (ev < t1)]
        agg.setdefault(("sig_rate", name), []).append(len(ev) / mins)
        agg.setdefault(("sig_episodes", name), []).append((int(np.sum(np.diff(ev) > 1000)) + 1 if len(ev) else 0) / mins)
        k = np.searchsorted(ev, probe, "right")
        wait = np.where(k < len(ev), ev[np.clip(k, 0, len(ev) - 1)] - probe if len(ev) else 1e12, 1e12)
        rem = t1 - probe
        for hold in (15000, 60000, 180000):
            ok = rem >= hold
            if ok.any():
                agg.setdefault(("sig_surv", name, hold), []).extend((wait[ok] > hold).astype(int).tolist())


def mid_events(mk_list, thr=50):
    """{(book name): (up, down)} mid-move events of each book."""
    return alt_per_alt(mk_list, thr)


def leads(books, t0, t1, agg):
    """books: name -> (up, down) event times. For each pair (A, B): debounced A events (1 s quiet), first same-direction B event
    within +-1 s -> lag = tB - tA."""
    pairs = [("btc5", "btc15"), ("btc15", "btc5"), ("alt5", "btc5"), ("btc5", "alt5"), ("alt5", "btc15"), ("alt15", "btc15"),
             ("btc15", "alt15")]
    for a, bname in pairs:
        if a not in books or bname not in books:
            continue
        for d in (0, 1):
            A = np.asarray(books[a][d])
            A = A[(A >= t0) & (A < t1)]
            if not len(A):
                continue
            A = A[np.r_[True, np.diff(A) > 1000]]  # episode starts: first move after 1 s of quiet
            Bv = np.asarray(books[bname][d])
            Bv = Bv[np.r_[True, np.diff(Bv) > 1000]] if len(Bv) else Bv
            for t in A:
                j = np.searchsorted(Bv, t)
                c = [Bv[i] for i in (j - 1, j) if 0 <= i < len(Bv) and abs(Bv[i] - t) <= 1000]
                if c:
                    agg.setdefault(("lead", a, bname), []).append(float(min(c, key=lambda v: abs(v - t)) - t))
                else:
                    agg.setdefault(("lead_nomatch", a, bname), [0])[0] += 1


def main():
    day = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)
    agg = {}
    g5 = pmlat.hours(day, "btc", 5, with_act=False)
    ga5 = {a: pmlat.hours(day, a, 5, with_act=False) for a in ALTS}
    ga15 = {a: pmlat.hours(day, a, 15, with_act=False) for a in ALTS}
    cur = {}

    def adv(key, gen, h):
        while cur.get(key, (None,))[0] is None or cur[key][0] < h:
            try:
                cur[key] = next(gen)
            except StopIteration:
                cur[key] = (99, {})
        return cur[key][1] if cur[key][0] == h else {}

    for h, mk15 in pmlat.hours(day, "btc", 15, with_act=False):
        mk5 = adv("btc5", g5, h)
        a5 = {a: adv("a5" + a, ga5[a], h) for a in ALTS}
        a15 = {a: adv("a15" + a, ga15[a], h) for a in ALTS}
        for e, m in mk5.items():
            anatomy(m, 5, agg)
        for e, m in mk15.items():
            anatomy(m, 15, agg)
            alt15 = [a15[a][e] for a in ALTS if e in a15[a]]
            alt5 = [mm for a in ALTS for ee, mm in a5[a].items() if e <= ee < e + 900]
            btc5 = [mm for ee, mm in mk5.items() if e <= ee < e + 900]
            signals(m, alt15, alt5, btc5, agg)
            books = {"btc15": (mid_events([m])[0] if mid_events([m]) else (np.zeros(0), np.zeros(0)))}
            for name, lst in (("alt15", alt15), ("alt5", alt5), ("btc5", btc5)):
                pa = mid_events(lst)
                if pa:
                    books[name] = (union(pa, 0), union(pa, 1))
            leads(books, e * 1000, (e + 900) * 1000, agg)
        print(day, h, len(mk15), len(mk5), flush=True)
    with open(OUT / f"p1-{day}.pkl", "wb") as f:
        pickle.dump(agg, f)
    print("wrote", OUT / f"p1-{day}.pkl")


if __name__ == "__main__":
    main()
