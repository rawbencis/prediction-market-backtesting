"""D4 lead atlas (descriptive, no P&L). Every time is this box's receive clock (ms).

Feeds: Binance BTC spot trades (shadow-rec bn, src 0), Coinbase BTC-USD matches, Deribit BTC-PERPETUAL ticker mid
(100 ms channel), Chainlink BTC (RTDS), and the BTC 5m / 15m UP top of book (shadow-rec pm) of the live window.
  venue move : |p(t) / p(t - 500 ms) - 1| >= 2 bp, 2 s debounce per venue, signed.
  which first: for each move of venue A, the nearest same-sign move of venue B within +-1 s; lag = t_B - t_A.
  reprice    : after a venue move at t0, the first book row of the live window (mid 5-95c, >= 10 s left) whose UP mid
               moved >= 0.5c from its value at t0 in the move's direction, within 2 s; lead = t_reprice - t0.
  book move  : the live window's UP mid moves >= 1c within 500 ms (2 s debounce). Preceded by an external move if a
               same-sign Coinbase or Deribit move happened in the 2 s before it; lead = t_book - t_external.
usage: d4_atlas.py DAY [DAY ...] -> prints tables
"""
import gzip
import sys
from pathlib import Path

import numpy as np
import orjson

REC, FEEDS = Path.home() / "shadow-rec", Path.home() / "feeds-rec"
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])


def gz_rows(day, feed, h, needle, parse):
    f = FEEDS / day / f"{feed}-{h:02d}.jsonl.gz"
    t, p = [], []
    if not f.exists():
        return t, p
    try:
        with gzip.open(f, "rb") as g:
            for line in g:
                if needle not in line:
                    continue
                try:
                    m = orjson.loads(line)
                    v = parse(m)
                except Exception:
                    continue
                if v:
                    t.append(int(m["r"]) // 1_000_000)
                    p.append(v)
    except Exception:
        pass
    return t, p


def events(t, p, bp=2.0, win=500, deb=2000):
    t, p = np.asarray(t, np.int64), np.asarray(p, float)
    if len(t) < 2:
        return np.zeros(0, np.int64), np.zeros(0)
    o = np.argsort(t, kind="stable")
    t, p = t[o], p[o]
    j = np.searchsorted(t, t - win, "left")
    r = (p / p[j] - 1) * 1e4
    et, es, last = [], [], -10**15
    for i in np.flatnonzero(np.abs(r) >= bp):
        if t[i] - last >= deb:
            et.append(t[i]); es.append(np.sign(r[i])); last = t[i]
    return np.array(et, np.int64), np.array(es)


def q(v):
    v = np.asarray(v, float)
    return "n/a" if len(v) == 0 else "/".join(f"{x:.0f}" for x in np.percentile(v, [25, 50, 75]))


def main(days):
    V = {k: ([], []) for k in ("binance", "coinbase", "deribit", "chainlink")}
    books = {5: [], 15: []}
    for day in days:
        for h in range(24):
            f = REC / day / f"bn-{h:02d}.bin"
            if f.exists():
                x = np.fromfile(f, dtype=BN_DT)
                x = x[(x["src"] == 0) & (x["a"] == 0)]
                V["binance"][0].append(x["l"] // 1000); V["binance"][1].append(x["p"])
            t, p = gz_rows(day, "coinbase", h, b'"BTC-USD"', lambda m: float(m["price"]) if m.get("type") == "match" else None)
            V["coinbase"][0].append(np.array(t, np.int64)); V["coinbase"][1].append(np.array(p))
            t, p = gz_rows(day, "deribit", h, b"BTC-PERPETUAL", lambda m: (m["bid"] + m["ask"]) / 2 if m.get("bid") and m.get("ask") else None)
            V["deribit"][0].append(np.array(t, np.int64)); V["deribit"][1].append(np.array(p))
            t, p = gz_rows(day, "chainlink", h, b'"s":"btc"', lambda m: float(m["v"]))
            V["chainlink"][0].append(np.array(t, np.int64)); V["chainlink"][1].append(np.array(p))
            f = REC / day / f"pm-{h:02d}.bin"
            if f.exists():
                x = np.fromfile(f, dtype=PM_DT)
                x = x[(x["a"] == 0)]
                for tf in (5, 15):
                    y = x[x["tf"] == tf]
                    lm = y["l"] // 1000
                    live = (lm >= y["e"].astype(np.int64) * 1000) & (lm < (y["e"].astype(np.int64) + 60 * tf - 10) * 1000) & \
                           (y["b"] > 0) & (y["k"] > y["b"]) & (y["k"] < 10000)
                    y, lm = y[live], lm[live]
                    mid = (y["b"].astype(float) + y["k"]) / 2
                    ok = (mid >= 500) & (mid <= 9500)
                    lm, mid = lm[ok], mid[ok]
                    keep = np.r_[True, np.diff(mid) != 0]  # only rows where the mid changed (memory)
                    books[tf].append(np.stack([lm[keep].astype(float), mid[keep]], 1).astype(np.float64))
        print("loaded", day, file=sys.stderr, flush=True)
    EV = {k: events(np.concatenate(v[0]) if v[0] else [], np.concatenate(v[1]) if v[1] else []) for k, v in V.items()}
    print(f"Days: {', '.join(days)}. Moves >= 2 bp within 500 ms (2 s debounce): " +
          ", ".join(f"{k} {len(EV[k][0]):,}" for k in EV) + ".\n")
    print("| A -> B | A moves matched | share A first | lag t_B - t_A ms p25/p50/p75 |\n|---|---|---|---|")
    names = list(EV)
    for a in names:
        for b in names:
            if a >= b:
                continue
            ta, sa = EV[a]; tb, sb = EV[b]
            lags = []
            for t0, s0 in zip(ta, sa):
                lo, hi = np.searchsorted(tb, t0 - 1000), np.searchsorted(tb, t0 + 1000, "right")
                c = [(abs(tb[i] - t0), tb[i] - t0) for i in range(lo, hi) if sb[i] == s0]
                if c:
                    lags.append(min(c)[1])
            lags = np.array(lags)
            print(f"| {a} -> {b} | {len(lags):,} of {len(ta):,} | {np.mean(lags > 0) if len(lags) else float('nan'):.2f} | {q(lags)} |")
    for tf in (5, 15):
        B = np.concatenate(books[tf]) if books[tf] else np.zeros((0, 2))
        B = B[np.argsort(B[:, 0], kind="stable")]
        bt, bm = B[:, 0], B[:, 1]
        print(f"\n**BTC {tf}m book** ({len(bt):,} live-window rows). Lead of each venue move over the first book reprice "
              "(>= 0.5c in the move's direction within 2 s):\n")
        print("| venue | moves while a window is live | share followed by a reprice | lead ms p25/p50/p75 | share lead > 190 ms | > 90 ms |\n|---|---|---|---|---|---|")
        for v in names:
            te, se = EV[v]
            leads, n = [], 0
            for t0, s0 in zip(te, se):
                i0 = np.searchsorted(bt, t0, "right") - 1
                if i0 < 0 or t0 - bt[i0] > 5000:
                    continue
                n += 1
                m0 = bm[i0]
                i1 = np.searchsorted(bt, t0 + 2000, "right")
                seg = np.flatnonzero(s0 * (bm[i0 + 1:i1] - m0) >= 50)
                if len(seg):
                    leads.append(bt[i0 + 1 + seg[0]] - t0)
            leads = np.array(leads)
            print(f"| {v} | {n:,} | {len(leads) / max(n, 1):.2f} | {q(leads)} | {np.mean(leads > 190) if len(leads) else 0:.2f} | {np.mean(leads > 90) if len(leads) else 0:.2f} |")
        # book moves: |mid(t) - mid(t - 500)| >= 1c
        j = np.searchsorted(bt, bt - 500, "left")
        dm = bm - bm[j]
        bmt, bms, last = [], [], -10**15
        for i in np.flatnonzero(np.abs(dm) >= 100):
            if bt[i] - last >= 2000:
                bmt.append(bt[i]); bms.append(np.sign(dm[i])); last = bt[i]
        ext_t = np.concatenate([EV["coinbase"][0], EV["deribit"][0]])
        ext_s = np.concatenate([EV["coinbase"][1], EV["deribit"][1]])
        o = np.argsort(ext_t, kind="stable"); ext_t, ext_s = ext_t[o], ext_s[o]
        bin_t, bin_s = EV["binance"]
        res = {"coinbase/deribit": [], "binance": []}
        for t1, s1 in zip(bmt, bms):
            for nm, (et, es) in (("coinbase/deribit", (ext_t, ext_s)), ("binance", (bin_t, bin_s))):
                lo, hi = np.searchsorted(et, t1 - 2000), np.searchsorted(et, t1, "right")
                c = [t1 - et[i] for i in range(lo, hi) if es[i] == s1]
                res[nm].append(max(c) if c else np.nan)  # the earliest same-sign move in the 2 s before
        print(f"\nBTC {tf}m book moves (>= 1c in 500 ms): {len(bmt):,}.\n")
        print("| preceded by | share with a same-sign move in the prior 2 s | lead ms p25/p50/p75 | share of all book moves with lead > 190 ms | > 90 ms |\n|---|---|---|---|---|")
        for nm, v in res.items():
            v = np.array(v, float); ok = np.isfinite(v)
            print(f"| {nm} | {ok.mean():.2f} | {q(v[ok])} | {np.mean(v > 190):.2f} | {np.mean(v > 90):.2f} |")


if __name__ == "__main__":
    main(sys.argv[1:])
