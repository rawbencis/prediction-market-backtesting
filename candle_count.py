#!/usr/bin/env python3
"""Candle count: do the outcomes of earlier BTC 5m up/down windows predict the next one, and is it already in the
opening price?  Polymarket data only (Gamma winners; optional June-July book cache from same_token_wf2 prep).

  candle_count.py fetch --start 2026-06-01 --end 2026-11-17 --out ~/cc-outcomes.csv [--seed ~/stwf2-cache/settlements.csv]
  candle_count.py eval  --outcomes ~/cc-outcomes.csv [--split 2026-08-01] [--cache ~/stwf2-cache] [--rec ~/shadow-rec]
--cache: June-July books (same_token_wf2 prep). --rec: shadow_rec recordings (BTC 5m rows only, one hour file at a time).
  candle_count.py dip   --outcomes ~/cc-outcomes.csv [--cache ...] [--rec ...]
dip: after a run of equal closes, rest a cheap bid on the reversal side. Is a fill there worth more than its price,
and what does "bet again until it wins" do to the result?

A 1c/share edge is a 1-point shift in win probability, so everything is reported in points with its standard error.
"""
import argparse, csv, json, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

STEP, TF, ASSET, AIDX = 300, 5, "btc", 0     # window length (s), minutes, asset; set by --tf / --asset
NS = (1, 2, 3, 5, 10, 20, 50, 100, 288)   # rolling lengths, in 5m windows (288 = one day)
GAMMA = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&"


def ep(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def read(p):
    return {int(r["slug"].rsplit("-", 1)[1]): r["winner"] for r in csv.DictReader(Path(p).open())} if Path(p).exists() else {}


def cmd_fetch(a):
    have = {}
    for p in (a.seed or []) + [a.out]:
        have.update(read(p))
    todo = [e for e in range(ep(a.start), ep(a.end) + 86400, STEP) if e not in have and e + 900 < time.time()]
    print(f"have {len(have)}, to fetch {len(todo)}", flush=True)

    def get(batch):
        url = GAMMA + "&".join(f"slug={ASSET}-updown-{TF}m-{e}" for e in batch)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
            return json.load(r)

    def save():
        with Path(a.out).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "winner"])
            w.writerows((f"{ASSET}-updown-{TF}m-{e}", v) for e, v in sorted(have.items()))

    for i in range(0, len(todo), 50):
        try:
            for m in get(todo[i:i + 50]):
                names, px = [n.upper() for n in json.loads(m["outcomes"])], [float(v) for v in json.loads(m["outcomePrices"])]
                if m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(px) >= 0.99 and min(px) <= 0.01:
                    have[int(m["slug"].rsplit("-", 1)[1])] = names[px.index(max(px))]
        except Exception as e:  # noqa: BLE001
            print("fetch failed", i, repr(e)[:120], flush=True)
        if i % 5000 == 0:
            save()
            print(f"{i}/{len(todo)} have {len(have)}", flush=True)
        time.sleep(a.sleep)
    save()
    print(f"wrote {a.out}: {len(have)} winners")


# ---------------------------------------------------------------- pure (unit-tested)
def rolling(E, U, n):
    """-> (idx, f): windows whose n predecessors are all present, and the UP share of those n."""
    c = np.r_[0, np.cumsum(U)]
    i = np.arange(n, len(E))
    i = i[E[i] - E[i - n] == STEP * n]
    return i, (c[i] - c[i - n]) / n


def pts(k, n):
    """Win rate minus 50%, in points, and its standard error."""
    return (100 * (k / n - 0.5), 50 / np.sqrt(n)) if n else (float("nan"), float("nan"))


def bets(E, U, n, d):
    """d=+1: bet with the majority of the last n; d=-1: against. -> (idx, win 0/1); ties place no bet."""
    i, f = rolling(E, U, n)
    s = np.sign(f - 0.5) * d
    k = s != 0
    return i[k], (U[i[k]] == (s[k] > 0)).astype(int)


def streaks(E, U, cap=7):
    """-> {L: (n, continued)}: after exactly L equal outcomes in a row (L capped), how often the next was the same."""
    out, run = {}, 1
    for i in range(1, len(U)):
        if E[i] - E[i - 1] != STEP:
            run = 0   # unknown history: the run restarts with this window
        elif run:
            L = min(run, cap)
            n, k = out.get(L, (0, 0))
            out[L] = (n + 1, k + int(U[i] == U[i - 1]))
        run = run + 1 if run and E[i] - E[i - 1] == STEP and U[i] == U[i - 1] else 1
    return out


def walk_forward(E, U, look=30, cells=None):
    """Each day: pick the (n, d) cell with the most net wins over the previous `look` days, bet it that day.
    -> (day index per bet, win 0/1 per bet, picks)."""
    cells = cells or [(n, d) for n in NS for d in (1, -1)]
    day = E // 86400
    B = []
    for n, d in cells:
        i, w = bets(E, U, n, d)
        B.append((day[i], w))
    D, W, picks = [], [], []
    for t in np.unique(day):
        sc = [(2 * w[(dd >= t - look) & (dd < t)].sum() - ((dd >= t - look) & (dd < t)).sum()) for dd, w in B]
        if t - day.min() < look or max(sc) <= 0:
            continue
        b = int(np.argmax(sc))
        k = B[b][0] == t
        D.append(B[b][0][k]), W.append(B[b][1][k]), picks.append(cells[b])
    return (np.concatenate(D), np.concatenate(W), picks) if D else (np.array([]), np.array([]), [])


def day_ci(day, w, n=2000, seed=1):
    """90% interval of (win rate - 50%) in points, resampling days."""
    u, inv = np.unique(day, return_inverse=True)
    k, m = np.bincount(inv, w), np.bincount(inv)
    r = np.random.default_rng(seed).integers(0, len(u), (n, len(u)))
    v = 100 * (k[r].sum(1) / m[r].sum(1) - 0.5)
    return round(float(np.percentile(v, 5)), 2), round(float(np.percentile(v, 95)), 2)


def quote_at(m, s_us, age=2_000_000, gap=10_000_000):
    """UP bid/ask (dollars) standing s_us after the open. None if missing, crossed, the UP side was stale at the
    book's last event (as in same_token_wf2), or the book had no event at all for `gap` before s_us."""
    j = int(np.searchsorted(m["t"], s_us, "right")) - 1
    if (j < 0 or m["bid"][j, 0] < 0 or m["ask"][j, 0] <= m["bid"][j, 0] or m["t"][j] - m["seen"][j, 0] > age
            or s_us - m["t"][j] > gap):
        return None
    return m["bid"][j, 0] / 1000, m["ask"][j, 0] / 1000


PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])  # shadow_rec pm row; asset 0 = btc


def cache_quotes(cache, S):
    from same_token_wf2 import done_days, markets
    for ddir in done_days(Path(cache)):
        for e, m in markets(ddir):
            for s in S:
                q = quote_at(m, s * 1_000_000)
                if q:
                    yield e, s, q


def rec_quotes(root, S, gap_ms=15_000, files=None):
    """BTC 5m UP bid/ask standing s seconds after each window's open, from shadow_rec hour files. The quote is the
    last row at or before that instant (rows from just before the open count); None-like cases are skipped:
    a gap marker (bid = ask = 0), a crossed or one-sided book, or no row in the gap_ms before it."""
    best = {}
    for f in files or sorted(Path(root).glob("????-??-??/pm-*.bin")):
        r = np.fromfile(f, dtype=PM_DT, count=f.stat().st_size // PM_DT.itemsize)
        r = r[(r["a"] == AIDX) & (r["tf"] == TF)]
        e_ms = r["e"].astype(np.int64) * 1000
        for s in S:
            m = (r["x"] <= e_ms + s * 1000) & (r["x"] > e_ms + s * 1000 - gap_ms)
            q = r[m]
            q = q[np.lexsort((q["l"], q["x"], q["e"]))]
            last = np.r_[np.flatnonzero(np.diff(q["e"])), len(q) - 1] if len(q) else []
            for j in last:
                key, row = (int(q["e"][j]), s), (int(q["x"][j]), int(q["l"][j]), int(q["b"][j]), int(q["k"][j]))
                if key not in best or row[:2] > best[key][:2]:
                    best[key] = row
    for (e, s), (_, _, b, k) in sorted(best.items()):
        if 0 < b < k:
            yield e, s, (b / 1e4, k / 1e4)


# ---------------------------------------------------------------- dip: cheap resting bid on the reversal side
DIP_P = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)
DIP_T0 = 2000        # ms after the open: the bid is placed
NEVER = 10 ** 9


def window_dips(t, bid, ask, P=DIP_P, t0=DIP_T0):
    """One side of one window. t: ms after the open (sorted); bid/ask in dollars, nan = missing.
    -> (bid0, ask0, ts, tl): the quote standing at t0, and per price P the first time after t0 at which
       ts: the ask is at or below P (a resting bid at P has certainly been filled: 'strict'),
       tl: the best bid is below P (every bid at or above P is gone: 'loose').  NEVER if it does not happen."""
    j0 = int(np.searchsorted(t, t0, "right")) - 1
    if j0 < 0:
        return np.nan, np.nan, None, None
    def last(v):
        k = np.flatnonzero(~np.isnan(v[:j0 + 1]))
        return v[k[-1]] if len(k) else np.nan
    a = np.where(np.isnan(ask[j0 + 1:]), np.inf, ask[j0 + 1:])
    b = np.where(np.isnan(bid[j0 + 1:]), np.inf, bid[j0 + 1:])
    tt = t[j0 + 1:]
    ca, cb = (np.minimum.accumulate(a), np.minimum.accumulate(b)) if len(tt) else (a, b)
    def first(c, lim):
        k = int(np.argmax(c <= lim)) if len(c) and c[-1] <= lim else -1
        return int(tt[k]) if k >= 0 else NEVER
    return (last(bid), last(ask), np.array([first(ca, p + 1e-9) for p in P]), np.array([first(cb, p - 0.005) for p in P]))


def _both_sides(t, ub, ua, db=None, da=None):
    """-> {True: UP result, False: DOWN result}. Without a DOWN book it is the mirror of UP's."""
    db, da = (1 - ua, 1 - ub) if db is None else (db, da)
    return {True: window_dips(t, ub, ua), False: window_dips(t, db, da)}


def cache_paths(cache):
    from same_token_wf2 import done_days, markets
    nan = lambda v: np.where(v < 0, np.nan, v / 1000.0)  # noqa: E731
    for ddir in done_days(Path(cache)):
        for e, m in markets(ddir):
            yield e, _both_sides(m["t"] // 1000, nan(m["bid"][:, 0]), nan(m["ask"][:, 0]), nan(m["bid"][:, 1]), nan(m["ask"][:, 1]))


def rec_paths(root, files=None):
    """BTC 5m windows from shadow_rec hour files (UP book only). A window is emitted once a later hour is read."""
    acc = {}

    def emit(e):
        r = np.concatenate(acc.pop(e))
        r = r[np.lexsort((r["l"], r["x"]))]
        nz = lambda v: np.where(v == 0, np.nan, v / 1e4)  # noqa: E731   0 = no quote / gap marker
        return e, _both_sides(r["x"] - e * 1000, nz(r["b"].astype(float)), nz(r["k"].astype(float)))

    for f in files or sorted(Path(root).glob("????-??-??/pm-*.bin")):
        r = np.fromfile(f, dtype=PM_DT, count=f.stat().st_size // PM_DT.itemsize)
        r = r[(r["a"] == AIDX) & (r["tf"] == TF)]
        for e in np.unique(r["e"]):
            acc.setdefault(int(e), []).append(r[r["e"] == e])
        if len(r):
            for e in sorted(k for k in acc if k < int(r["e"].min()) - 900):
                yield emit(e)
    for e in sorted(acc):
        yield emit(e)


LB_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4")]
                 + [(f"{sd}{i}{f}", t) for sd in "bk" for i in (1, 2, 3) for f, t in (("", "<u2"), ("s", "<f4"))])   # lighter_rec book
LT_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("p", "<u2"), ("s", "<f4"),
                  ("buy", "u1"), ("aa", "<i8"), ("ba", "<i8"), ("id", "<i8")])                                      # lighter_rec trades


def lighter_paths(root):
    """Lighter up/down windows (one book, quoted as the UP price). The makers there are few and mostly post-only, so
    a quote reaching our price is not a fill. Fills come from recorded TAKER trades only:
      strict: a taker traded through our price (a sell below P for an UP bid; a buy above 1-P for a DOWN bid);
      loose:  a taker traded at our price or through it (our place in the queue is ignored).
    The quote at placement comes from the book."""
    rd = lambda kind, dt: [np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)  # noqa: E731
                           for f in sorted(Path(root).glob(f"????-??-??/{kind}-*.bin"))]
    cat = lambda v, dt: np.concatenate(v) if v else np.zeros(0, dt)  # noqa: E731
    LB, LT = cat(rd("lb", LB_DT), LB_DT), cat(rd("lt", LT_DT), LT_DT)
    LB, LT = LB[(LB["a"] == AIDX) & (LB["tf"] == TF)], LT[(LT["a"] == AIDX) & (LT["tf"] == TF)]
    LT = LT[np.sort(np.unique(LT["id"], return_index=True)[1])]
    nz = lambda v: np.where(v == 0, np.nan, v / 1e4)  # noqa: E731
    for e in np.unique(LB["e"]):
        e = int(e)
        b = LB[LB["e"] == e]
        b = b[np.lexsort((b["l"], b["x"]))]
        tb, ub, ua = b["x"] - e * 1000, nz(b["b1"].astype(float)), nz(b["k1"].astype(float))
        tr = LT[LT["e"] == e]
        tr = tr[np.argsort(tr["T"], kind="stable")]
        tt, px, buy = tr["T"] - e * 1000, tr["p"] / 1e4, tr["buy"] == 1
        out = {}
        for side in (True, False):
            b0, a0, _, _ = window_dips(tb, ub, ua) if side else window_dips(tb, 1 - ua, 1 - ub)
            p = px[~buy] if side else 1 - px[buy]          # the price a taker sold this side at
            t = tt[~buy] if side else tt[buy]
            k = t > DIP_T0
            # a print at p acts like "ask = p + 1c, bid = p - 1c": strict needs p <= P - 1c, loose p <= P
            _, _, ts, tl = window_dips(np.r_[0, t[k]], np.r_[np.nan, p[k] - 0.01], np.r_[np.nan, p[k] + 0.01], t0=0)
            out[side] = (np.inf if not np.isnan(a0) else np.nan, a0, ts, tl)
        yield e, out


def streak_before(O, e, cap=8):
    """Length of the run of equal outcomes ending with the window before e (0 if that window is unknown)."""
    if e - STEP not in O:
        return 0
    L = 1
    while L < cap and O.get(e - STEP * (L + 1)) == O[e - STEP]:
        L += 1
    return L


def dip_signals(O):
    """name -> f(e) -> True (bid UP) / False (bid DOWN) / None (no order)."""
    rev = lambda e: O[e - STEP] != "UP"  # noqa: E731   the side that lost the previous window
    def run(lo, hi=99, with_it=False):
        return lambda e: (None if not lo <= streak_before(O, e) <= hi else (not rev(e)) if with_it else rev(e))
    def last5(e):
        v = [O.get(e - STEP * k) for k in range(1, 6)]
        n = sum(x == "UP" for x in v)
        return None if None in v or n in (2, 3) else n <= 1
    return {"any window, against the previous close": run(1), "run of 1-2, against it (no signal)": run(1, 2),
            "run of 2+, against it": run(2), "run of 3+, against it": run(3), "run of 4+, against it": run(4),
            "run of 5+, against it": run(5), "last 5 were 4-1 or 5-0, against": last5,
            "run of 3+, WITH it (placebo)": run(3, with_it=True)}


def dip_rows(W, O, sig, pi, t1=None):
    """-> list of (epoch, status, strict fill, loose fill, win) for one signal and price index.
    status: 'placed', 'below' (the ask was already at or under P at placement: no order), 'nodata'."""
    out, t1 = [], (STEP - 30) * 1000 if t1 is None else t1
    for e in sorted(W):
        if e not in O or e - STEP not in O:
            continue
        side = sig(e)
        if side is None:
            continue
        bid0, ask0, ts, tl = W[e][side]
        if ts is None or np.isnan(ask0):
            out.append((e, "nodata", False, False, False))
            continue
        P = DIP_P[pi]
        if ask0 <= P + 1e-9:
            out.append((e, "below", False, False, False))
            continue
        strict = ts[pi] <= t1
        loose = strict or (not np.isnan(bid0) and bid0 >= P - 1e-9 and tl[pi] <= t1)
        out.append((e, "placed", bool(strict), bool(loose), (O[e] == "UP") == side))
    return out


def dip_stat(rows, col):
    """-> (placed, fills, wins, win rate if not filled). col 2 = strict, 3 = loose."""
    pl = [r for r in rows if r[1] == "placed"]
    f = [r for r in pl if r[col]]
    nf = [r for r in pl if not r[col]]
    return len(pl), len(f), sum(r[4] for r in f), (sum(r[4] for r in nf) / len(nf) if nf else float("nan"))


def dip_sequences(W, O, K, pi, scheme, cap, t1=None):
    """Bet the reversal side at P in every window of a run of K or more equal closes, until the run breaks.
    scheme 'flat': 1 unit each time. 'recover': sized so that a win pays back the sequence's losses plus one
    unit's profit; after `cap` losing fills the sequence is abandoned. Strict fills.
    -> dict(seqs, bets, fills, staked, pnl, worst, max_units, unrecovered), in units of the first bet's shares."""
    P = DIP_P[pi]
    sig = lambda e: None if streak_before(O, e) < K else O[e - STEP] != "UP"  # noqa: E731
    R = {r[0]: r for r in dip_rows(W, O, sig, pi, t1)}
    out = dict(seqs=0, bets=0, fills=0, staked=0.0, pnl=0.0, worst=0.0, max_units=0.0, unrecovered=0)
    lost, nloss = 0.0, 0

    def close():
        nonlocal lost, nloss
        if lost > 0:
            out["unrecovered"] += 1
            out["worst"] = min(out["worst"], -lost)
        lost, nloss = 0.0, 0

    for e in sorted(W):
        L = streak_before(O, e)
        if L <= K:            # the run broke, or a new run has just reached K: the old sequence is over
            close()
        if L < K:
            continue
        out["seqs"] += L == K
        r = R.get(e)
        if r is None or r[1] != "placed" or nloss >= cap:
            continue
        units = 1.0 if scheme == "flat" else (lost + (1 - P)) / (1 - P)
        out["bets"] += 1
        if not r[2]:
            continue
        out["fills"] += 1
        out["staked"] += units * P
        out["max_units"] = max(out["max_units"], units)
        if r[4]:
            out["pnl"] += units * (1 - P)
            lost, nloss = 0.0, 0          # won: the sequence's losses are recovered (recover) or simply closed (flat)
        else:
            out["pnl"] -= units * P
            lost += units * P
            nloss += 1
    close()
    return out


def cmd_dip(a):
    O = read(a.outcomes)
    book = "strict: the ask reached P (certain fill). loose: the best bid fell below P (likely fill)."
    for src, it, how in (("June-July book cache", a.cache and cache_paths(a.cache), book),
                         ("recordings " + str(a.rec), a.rec and rec_paths(a.rec), book),
                         ("Lighter " + str(a.lighter), a.lighter and lighter_paths(a.lighter),
                          "Fills from recorded taker trades only. strict: a taker traded through P. loose: at P or through.")):
        if not it:
            continue
        W = {e: w for e, w in it if e in O}
        print(f"\n=== {src}, {ASSET} {TF}m: {len(W)} windows with a known outcome. A bid of P on the reversal side rests from 2 s"
              f"\n    after the open to 30 s before the close. Edge = wins - P, in cents per filled share, +- 1 SE."
              f"\n    {how} No fee, no rebate (maker).")
        S = dip_signals(O)
        for name, sig in S.items():
            print(f"\n  {name}")
            print("     P    orders  skipped(already<=P)  fill%   wins/fills   edge strict      | fill%  edge loose       | wins if NOT filled")
            for pi, P in enumerate(DIP_P):
                rows = dip_rows(W, O, sig, pi)
                n, f, w, nfw = dip_stat(rows, 2)
                n2, f2, w2, _ = dip_stat(rows, 3)
                if not f:
                    continue
                e1, s1 = 100 * (w / f - P), 100 * np.sqrt(max(w / f * (1 - w / f), 1e-9) / f)
                e2, s2 = 100 * (w2 / f2 - P), 100 * np.sqrt(max(w2 / f2 * (1 - w2 / f2), 1e-9) / f2)
                print(f"     {P:.2f} {n:7d} {sum(r[1] == 'below' for r in rows):7d}            {100 * f / n:5.1f}  {w:5d}/{f:<5d}  {e1:+6.2f} +-{s1:4.2f} z{e1 / s1:+5.1f} | {100 * f2 / n2:5.1f} {e2:+6.2f} +-{s2:4.2f} | {100 * nfw:5.1f}%")
        print("\n  By how long the bid rests (run of 3+, against it; strict): edge in cents/share (fills)")
        print("     P     " + "".join(f"{lab:>18s}" for lab in ("first 20%", "first half", "to 60 s left", "to 30 s left", "to 5 s left")))
        for pi, P in enumerate(DIP_P):
            row = f"     {P:.2f}  "
            for t1 in (STEP * 200, STEP * 500, (STEP - 60) * 1000, (STEP - 30) * 1000, (STEP - 5) * 1000):
                n, f, w, _ = dip_stat(dip_rows(W, O, S["run of 3+, against it"], pi, t1), 2)
                row += f"{100 * (w / f - P):+8.2f} ({f:5d})  " if f else f"{'-':>18s}"
            print(row)
        print("\n  'Bet again until it wins' (run of 3+, strict fills, to 30 s left). Units: 1 = the first bet's shares."
              "\n  recover = each bet sized to win back the sequence's losses plus one unit's profit; stop after 4 losing fills.")
        print("     P    scheme    sequences  bets  fills  staked   P&L     P&L per $ staked   worst sequence  biggest bet  ended unrecovered")
        for pi, P in enumerate(DIP_P):
            for scheme, cap in (("flat", 99), ("recover", 4)):
                d = dip_sequences(W, O, 3, pi, scheme, cap)
                if d["fills"]:
                    print(f"     {P:.2f} {scheme:8s} {d['seqs']:9d} {d['bets']:6d} {d['fills']:6d} {d['staked']:7.1f} {d['pnl']:+8.1f}   {100 * d['pnl'] / d['staked']:+7.2f} c            "
                          f"{d['worst']:+8.2f}    {d['max_units']:6.2f}x      {d['unrecovered']:5d}")
        print(f"  cells: about {len(S) * len(DIP_P) + 30}; expect ~{(len(S) * len(DIP_P) + 30) // 20} beyond 2 SE by chance.")


# ---------------------------------------------------------------- report
def cmd_eval(a):
    O = read(a.outcomes)
    E = np.array(sorted(O))
    U = np.array([O[e] == "UP" for e in E]).astype(int)
    split = ep(a.split)
    parts = [("all", E > 0), (f"before {a.split}", E < split), (f"from {a.split}", E >= split)]
    print(f"markets {len(E)}, {datetime.fromtimestamp(E[0], timezone.utc):%F} .. {datetime.fromtimestamp(E[-1], timezone.utc):%F}, "
          f"UP won {U.mean():.4f}. 1 point = 1c/share at a fair fill. Cells looked at below: {2 * len(NS) + 7}")

    print("\nA. Bet AGAINST the majority of the last N windows (negative = betting WITH it wins). points +- SE, z")
    print("   N    " + "".join(f"{lab:>28s}" for lab, _ in parts))
    for n in NS:
        i, w = bets(E, U, n, -1)
        row = f"   {n:<4d} "
        for _, msk in parts:
            k = msk[i]
            p, se = pts(w[k].sum(), k.sum())
            row += f"{p:+7.2f} +-{se:4.2f} z{p / se:+5.1f} n{k.sum():6d}"
        print(row)

    print("\nB. After L equal outcomes in a row: how often the next one is the same, in points vs 50% +- SE (n)."
          "\n   Negative = the streak tends to break. L2 is 'two the same, what about the third'.")
    for lab, msk in parts:
        S = streaks(E[msk], U[msk])
        print(f"   {lab:18s} " + " | ".join(f"L{L}{'+' if L == 7 else ''} {pts(k, n)[0]:+.2f}+-{pts(k, n)[1]:.2f} ({n})" for L, (n, k) in sorted(S.items())))

    print("\nC. By month, betting against the previous window (N=1): points, n")
    i, w = bets(E, U, 1, -1)
    mon = np.array([datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m") for e in E[i]])
    print("   " + " | ".join(f"{m_} {pts(w[mon == m_].sum(), (mon == m_).sum())[0]:+.2f} ({(mon == m_).sum()})" for m_ in sorted(set(mon))))
    day = E[i] // 86400
    print(f"   all: {pts(w.sum(), len(w))[0]:+.2f}, 90% by days {day_ci(day, w)}; from {a.split}: "
          f"{pts(w[E[i] >= split].sum(), (E[i] >= split).sum())[0]:+.2f}, 90% {day_ci(day[E[i] >= split], w[E[i] >= split])}")

    print("\nD. Walk-forward: each day bet the (N, with/against) cell that won most over the previous 30 days")
    D, W, picks = walk_forward(E, U)
    if len(W):
        import collections
        print(f"   bets {len(W)}, points {pts(W.sum(), len(W))[0]:+.2f} +-{pts(W.sum(), len(W))[1]:.2f}, 90% by days {day_ci(D, W)}; "
              f"most picked {collections.Counter(picks).most_common(4)}")

    print("\nG. Dose: bet against the majority of the last N, by how lopsided the count was (majority's share)."
          f"\n   points +- SE (n): all | from {a.split}. A real effect should grow with the share.")
    for n in (3, 5, 10, 20, 50):
        i, f = rolling(E, U, n)
        lop, win = np.abs(f - 0.5) * 2, (U[i] == (f < 0.5)).astype(int)
        row = f"   N={n:<3d}"
        for lo, hi, lab in ((0, .25, "51-62%"), (.25, .5, "63-75%"), (.5, .75, "76-87%"), (.75, 1, "88-100%")):
            k = (lop > lo) & (lop <= hi)
            k2 = k & (E[i] >= split)
            if k.sum() >= 200:
                row += f" | {lab} {pts(win[k].sum(), k.sum())[0]:+.2f}+-{pts(0, k.sum())[1]:.2f} ({k.sum()}) | {pts(win[k2].sum(), k2.sum())[0]:+.2f}+-{pts(0, max(1, k2.sum()))[1]:.2f}"
        print(row)

    S = (1, 5, 30)   # 1 s: the cleanest read of the book's lean, before the window's own price move adds noise
    for src, it in (("June-July book cache", a.cache and cache_quotes(a.cache, S)),
                    ("recordings " + str(a.rec), a.rec and rec_quotes(a.rec, S))):
        if not it:
            continue

        def side_map(n, lo=0.0):
            """epoch -> True if the bet is UP: against the majority of the last n, where lopsidedness > lo."""
            i, f = rolling(E, U, n)
            k = np.abs(f - 0.5) * 2 > lo
            return dict(zip(E[i[k]].tolist(), (f[k] < 0.5).tolist()))

        SIG = {"against the previous window": side_map(1), "against majority of last 3": side_map(3),
               "against majority of last 5": side_map(5), "  last 5 was 4-1 or 5-0": side_map(5, 0.5),
               "against majority of last 20": side_map(20), "  last 20 was 13-7 or more": side_map(20, 0.25),
               "against majority of last 50": side_map(50)}
        fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731
        R, RS = {(g, s): [] for g in SIG for s in S}, []
        for e, s, q in it:
            if e not in O:
                continue
            up = O[e] == "UP"
            for g, mp in SIG.items():
                if e in mp:
                    b, k = q if mp[e] else (1 - q[1], 1 - q[0])   # the chosen side's bid, ask
                    R[(g, s)].append((b, k, int(up == mp[e])))
            if s == 1 and e - STEP in O:
                L = 1   # length of the streak the previous window ended, capped at 5
                while L < 5 and O.get(e - STEP * (L + 1)) == O[e - STEP]:
                    L += 1
                pu = O[e - STEP] == "UP"
                b, k = (1 - q[1], 1 - q[0]) if pu else q
                RS.append((b, k, int(up != pu), L))

        def line(lab, rows):
            x = np.array(rows, float)
            mid, win = (x[:, 0] + x[:, 1]) / 2, x[:, 2]
            return (f"   {lab:30s} n {len(x):6d}   its mid {mid.mean():.4f} +-{mid.std() / np.sqrt(len(x)):.4f}   wins {win.mean():.4f}   "
                    f"edge at mid {100 * (win - mid).mean():+.2f} +-{100 * (win - mid).std() / np.sqrt(len(x)):.2f}   "
                    f"at its bid {100 * (win - x[:, 0]).mean():+.2f}   at its ask+fee {100 * (win - x[:, 1] - fee(x[:, 1])).mean():+.2f}")

        print(f"\nE. Is it priced? The side each rule would buy, as quoted after the open ({src}). 'its mid' above"
              "\n   0.5000 means the book already leans that way; 'edge at mid' is what is left, in points.")
        for s in S:
            print(f"   -- {s} s after the open")
            for g in SIG:
                if R[(g, s)]:
                    print(line(g, R[(g, s)]))
        print("\nF. The side AGAINST the streak, 1 s after the open, by the length of the streak (5 = five or more)")
        for L in (1, 2, 3, 4, 5):
            rows = [r[:3] for r in RS if r[3] == L]
            if rows:
                print(line(f"streak {L}{'+' if L == 5 else ''}", rows))
        rows = [r[:3] for r in RS if r[3] >= 3]
        if rows:
            print(line("streak 3 or more", rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch")
    f.add_argument("--start", required=True), f.add_argument("--end", required=True)
    f.add_argument("--out", type=Path, required=True), f.add_argument("--seed", type=Path, action="append")
    f.add_argument("--sleep", type=float, default=0.15)
    f.set_defaults(fn=cmd_fetch)
    e = sp.add_parser("eval")
    e.add_argument("--outcomes", type=Path, required=True), e.add_argument("--split", default="2026-08-01")
    e.add_argument("--cache", type=Path), e.add_argument("--rec", type=Path)
    e.set_defaults(fn=cmd_eval)
    d = sp.add_parser("dip")
    d.add_argument("--outcomes", type=Path, required=True), d.add_argument("--cache", type=Path), d.add_argument("--rec", type=Path)
    d.add_argument("--lighter", type=Path)
    d.set_defaults(fn=cmd_dip)
    for q in (f, e, d):
        q.add_argument("--asset", default="btc", choices=("btc", "eth", "sol", "xrp", "doge")), q.add_argument("--tf", type=int, default=5)
    a = ap.parse_args()
    ASSET, TF = a.asset, a.tf
    STEP, AIDX = TF * 60, ("btc", "eth", "sol", "xrp", "doge").index(ASSET)
    a.fn(a)
