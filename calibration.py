"""Calibration by price and time left, BTC 5m up/down. One pass over the same_token_wf2 npz cache.

Taker buy of one token at its ask. Decision on the quote at time ts (bucket = a0),
fill at the ask in force at ts+250 ms with >= 5 shares shown (a1).
  kind 0: snapshots every 10 s (primary: 60/120/180/240/270/290 s)
  kind 1: first time the token's ask crosses up through 600/700/800 milli (after being below)
Net edge per share (milli) = 1000*won - a1 - fee(a1), fee = 0.07*p*(1-p) (crypto taker, maker 0).
A quote counts only if both books are valid and mirrored (ask_U == 1000-bid_D and vice versa);
otherwise the record is censored (st=2) and reported per cell, not silently dropped.

Pre-registered rule (fixed before any run):
  candidate cell: n_pre >= 300, net 90% day-bootstrap CI lower bound > 0, censored share < 5%
  confirmed: holdout (>= 2026-07-19) n >= 100 and net mean >= +1.0c/share
  the count of candidates must also clearly exceed the ~5% false-positive rate of cells tested
"""

import argparse
import csv
import json
import warnings
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from same_token_wf2 import GAP_US, LAT_US, MKT_US, SIZE, done_days, markets, settlements

FEE = 0.07
SNAP = (60, 120, 180, 240, 270, 290)
DENSE = tuple(range(10, 300, 10))
XS = (600, 700, 800)
EDGES = np.r_[np.arange(0, 1000, 50), 1001]  # 5c buckets on a0
NB = len(EDGES) - 1
HOLDOUT = "2026-07-19"
BAD = ("2026-06-01", "2026-06-02", "2026-06-03")  # desync burst flagged in pair-gap scan
MIN_PRE, MIN_HOLD, MAX_CENS, MIN_NET = 300, 100, 0.05, 10.0
EP, KIND, KEY, TOK, T, A0, B0, A1, SZ, ST, WON = range(11)
# st: 0 ok, 1 no valid quote at decision, 2 invalid/unmirrored at fill, 3 size<5 at fill, 4 fill after close


def fee(a):
    return FEE * a * (1000 - a) / 1000


def records(m, W):
    t = m["t"]
    if len(t) < 2 or W is None:
        return np.zeros((0, 10))
    b, a, sz = m["bid"].astype(np.int64), m["ask"].astype(np.int64), m["asz"]
    v = (b >= 0).all(1) & (a > 0).all(1) & (a[:, 0] == 1000 - b[:, 1]) & (a[:, 1] == 1000 - b[:, 0])
    out = []

    def fill(k, ts, a0, b0, kind, key):
        te = ts + LAT_US
        st, a1, s1 = 4, -1, 0.0
        if te < MKT_US:
            x = int(np.searchsorted(t, te, "right")) - 1
            if not v[x] or te - t[x] > GAP_US:
                st = 2
            else:
                st, a1, s1 = (0 if sz[x, k] >= SIZE else 3), a[x, k], sz[x, k]
        out.append((kind, key, k, ts, a0, b0, a1, s1, st, int(W == k)))

    for s in DENSE:
        ts = s * 1_000_000
        d = int(np.searchsorted(t, ts, "right")) - 1
        for k in (0, 1):
            if d < 0 or not v[d] or ts - t[d] > GAP_US:
                out.append((0, s, k, ts, -1, -1, -1, 0.0, 1, int(W == k)))
            else:
                fill(k, ts, a[d, k], b[d, k], 0, s)
    vi = np.flatnonzero(v)
    for k in (0, 1):
        ak = a[vi, k]
        for xx in XS:
            lo = np.flatnonzero(ak < xx)
            if not len(lo):
                continue
            hi = np.flatnonzero(ak[lo[0] :] >= xx)
            if not len(hi):
                continue
            j = vi[lo[0] + hi[0]]
            fill(k, int(t[j]), a[j, k], b[j, k], 1, xx)
    return np.array(out, float)


def _day(args):
    ddir, out, sett = args
    R, nw = [], 0
    for e, m in markets(ddir):
        W = sett.get(e)
        nw += W is None
        r = records(m, W)
        if len(r):
            R.append(np.c_[np.full(len(r), e), r])
    if R:
        np.savez_compressed(out / f"{ddir.name}.npz", r=np.concatenate(R))
    return ddir.name, len(R), nw


def cmd_scan(a):
    a.out.mkdir(parents=True, exist_ok=True)
    sett = settlements(a.cache)
    days = done_days(a.cache)[: a.days or None]
    jobs = [(d, a.out, sett) for d in days if a.force or not (a.out / f"{d.name}.npz").exists()]
    with Pool(a.workers) as p:
        for d, k, nw in p.imap_unordered(_day, jobs):
            print(d, k, "no_winner", nw, flush=True)


# ---------------------------------------------------------------- report


def boot(S, N, B=2000, seed=0):
    """Day-cluster bootstrap of sum(S)/sum(N) per column. S,N: (days, cells)."""
    nd = len(S)
    if nd < 2:
        return np.full((2, S.shape[1]), np.nan)
    i = np.random.default_rng(seed).integers(0, nd, (B, nd))
    w = np.zeros((B, nd))
    np.add.at(w, (np.arange(B)[:, None], i), 1)
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = (w @ S) / (w @ N)
        return np.nanpercentile(m, [5, 95], axis=0)


def table(R, D, nd, days_sel, gid, ng):
    """Per-day sums for cells gid in [0, ng). Returns dict of (days, ng) arrays over selected days."""
    a1 = R[:, A1]
    vals = {
        "n": np.ones(len(R)),
        "net": 1000 * R[:, WON] - a1 - fee(a1),
        "gross": 1000 * R[:, WON] - a1,
        "won": R[:, WON],
        "a1": a1,
        "mk": 1000 * R[:, WON] - R[:, B0],  # unconditional maker-at-bid, ignores adverse selection
        "fee": fee(a1),
    }
    idx = D * ng + gid
    out = {k: np.bincount(idx, v, nd * ng).reshape(nd, ng)[days_sel] for k, v in vals.items()}
    return out


def summarize(R, D, days, gid, labels, tag):
    nd, ng = len(days), len(labels)
    dd = np.array(days)
    pre, hold = dd < HOLDOUT, dd >= HOLDOUT
    preX = pre & ~np.isin(dd, BAD)
    ok = R[:, ST] == 0
    Tp, Th, Tx = (table(R[ok], D[ok], nd, s, gid[ok], ng) for s in (pre, hold, preX))
    ci_p = boot(Tp["net"], Tp["n"])
    ci_h = boot(Th["net"], Th["n"], seed=1)
    # censoring: decision valid, fill invalid (st 2) vs fill attempted (st 0/2/3)
    att = np.isin(R[:, ST], (0, 2, 3))
    ncens = np.bincount(gid[att], R[att, ST] == 2, ng)
    natt = np.bincount(gid[att], None, ng)
    wcens = np.bincount(gid[att & (R[:, ST] == 2)], R[att & (R[:, ST] == 2), WON], ng)
    nsmall = np.bincount(gid[att], R[att, ST] == 3, ng)
    rows = []
    for c in range(ng):
        n_p, n_h = Tp["n"][:, c].sum(), Th["n"][:, c].sum()
        if n_p + n_h == 0:
            continue

        def f(T, k, c=c):
            return T[k][:, c].sum() / max(T["n"][:, c].sum(), 1)

        r = {
            "set": tag,
            "cell": labels[c],
            "n_pre": int(n_p),
            "n_hold": int(n_h),
            "ask_c": round(f(Tp, "a1") / 10, 2),
            "win_pct": round(100 * f(Tp, "won"), 2),
            "gap_c": round(f(Tp, "gross") / 10, 2),
            "fee_c": round(f(Tp, "fee") / 10, 2),
            "net_pre_c": round(f(Tp, "net") / 10, 2),
            "net_pre_ci90": [round(float(x) / 10, 2) for x in ci_p[:, c]],
            "net_pre_exJun1_3_c": round(f(Tx, "net") / 10, 2),
            "net_hold_c": round(f(Th, "net") / 10, 2) if n_h else None,
            "net_hold_ci90": [round(float(x) / 10, 2) for x in ci_h[:, c]],
            "maker_at_bid_uncond_c": round(f(Tp, "mk") / 10, 2),
            "cens": round(ncens[c] / max(natt[c], 1), 4),
            "cens_win_pct": round(100 * wcens[c] / ncens[c], 1) if ncens[c] else None,
            "small": round(nsmall[c] / max(natt[c], 1), 4),
        }
        lo = r["net_pre_ci90"][0]
        r["candidate"] = bool(
            n_p >= MIN_PRE and np.isfinite(lo) and lo > 0 and r["cens"] < MAX_CENS
        )
        r["tested"] = bool(n_p >= MIN_PRE)
        r["confirmed"] = bool(
            r["candidate"]
            and n_h >= MIN_HOLD
            and r["net_hold_c"] is not None
            and r["net_hold_c"] * 10 >= MIN_NET
        )
        rows.append(r)
    return rows


def cmd_report(a):
    fs = sorted(a.out.glob("????-??-??.npz"))
    days = [f.stem for f in fs]
    Rs = [np.load(f)["r"] for f in fs]
    R = np.concatenate(Rs)
    D = np.concatenate([np.full(len(r), i) for i, r in enumerate(Rs)])
    bk = np.clip(np.searchsorted(EDGES, R[:, A0], "right") - 1, 0, NB - 1)
    blab = [f"{EDGES[i] / 10:.0f}-{min(EDGES[i + 1], 1000) / 10:.0f}c" for i in range(NB)]
    out = []
    # 1) snapshots, all dense seconds x 5c bucket (primary = SNAP rows)
    s0 = R[:, KIND] == 0
    sec = R[:, KEY].astype(int)
    gi = np.full(len(R), -1)
    gi[s0] = (sec[s0] // 10 - 1) * NB + bk[s0]
    lab = [f"t{s}s|{b}" for s in DENSE for b in blab]
    m = gi >= 0
    out += summarize(R[m], D[m], days, gi[m], lab, "snap")
    # 2) crossings: x x minute of crossing (bucket ~ x, so split by time instead)
    c1 = R[:, KIND] == 1
    xi = np.searchsorted(XS, R[:, KEY].astype(int))
    mi = np.minimum(R[:, T] // 60_000_000, 4).astype(int)
    gi = np.full(len(R), -1)
    gi[c1] = xi[c1] * 5 + mi[c1]
    lab = [f"x{x}|min{k}" for x in XS for k in range(5)]
    m = gi >= 0
    out += summarize(R[m], D[m], days, gi[m], lab, "cross")
    # 3) pooled rules per primary snapshot: buy every favourite (a0>=500) / every longshot
    gi = np.full(len(R), -1)
    for j, s in enumerate(SNAP):
        sel = s0 & (sec == s)
        gi[sel] = 2 * j + (R[sel, A0] >= 500)
    lab = [f"t{s}s|{w}" for s in SNAP for w in ("longshot", "favourite")]
    m = gi >= 0
    out += summarize(R[m], D[m], days, gi[m], lab, "pooled")
    # exploratory: same split by token (UP/DOWN asymmetry, e.g. BTC drift over the sample)
    gi[m] = gi[m] * 2 + R[m, TOK].astype(int)
    lab = [
        f"t{s}s|{w}|{k}" for s in SNAP for w in ("longshot", "favourite") for k in ("UP", "DOWN")
    ]
    out += summarize(R[m], D[m], days, gi[m], lab, "pooled_tok")

    with (a.out / "cells.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    st = np.bincount(R[:, ST].astype(int), None, 5)
    tested = [r for r in out if r["set"] in ("snap", "cross") and r["tested"]]
    cand = [r for r in tested if r["candidate"]]
    conf = [r for r in cand if r["confirmed"]]
    rep = {
        "days": len(days),
        "first": days[0],
        "last": days[-1],
        "markets": len(np.unique(R[:, EP])),
        "records": len(R),
        "status_counts": dict(
            zip(("ok", "no_quote", "censored", "small", "after_close"), map(int, st))
        ),
        "cells_tested": len(tested),
        "candidates": len(cand),
        "expected_false_pos": round(0.05 * len(tested), 1),
        "confirmed": [r["cell"] for r in conf],
    }
    (a.out / "report.json").write_text(
        json.dumps({"summary": rep, "cells": out}, indent=1, default=float)
    )

    hdr = f"{'cell':>18} {'n_pre':>6} {'n_hold':>6} {'ask':>6} {'win%':>6} {'gap':>6} {'fee':>5} {'net_pre':>7} {'ci90':>16} {'exJ1-3':>6} {'hold':>6} {'hold_ci90':>16} {'mk@bid':>6} {'cens':>6} {'cW%':>5}"

    def show(rs):
        print(hdr)
        for r in rs:
            if r["n_pre"] < a.min_n:
                continue
            flag = "**" if r["confirmed"] else ("* " if r["candidate"] else "  ")
            print(
                f"{flag}{r['cell']:>16} {r['n_pre']:>6} {r['n_hold']:>6} {r['ask_c']:>6} {r['win_pct']:>6} {r['gap_c']:>6} "
                f"{r['fee_c']:>5} {r['net_pre_c']:>7} {r['net_pre_ci90']!s:>16} {r['net_pre_exJun1_3_c']:>6} "
                f"{r['net_hold_c']!s:>6} {r['net_hold_ci90']!s:>16} {r['maker_at_bid_uncond_c']:>6} "
                f"{r['cens']:>6} {r['cens_win_pct']!s:>5}"
            )

    print(json.dumps(rep, indent=1))
    print(
        "\nunits: cents/share; gap = win - ask (pre-fee); net = after taker fee at fill ask; * candidate, ** confirmed"
    )
    for s in SNAP:
        print(f"\n== snapshot {s}s ({300 - s}s left) ==")
        show([r for r in out if r["set"] == "snap" and r["cell"].startswith(f"t{s}s|")])
    print("\n== first crossing of ask through x, by minute of crossing ==")
    show([r for r in out if r["set"] == "cross"])
    print("\n== pooled: buy every favourite / longshot at the snapshot ==")
    show([r for r in out if r["set"] == "pooled"])
    print("\n== pooled by token (exploratory) ==")
    show([r for r in out if r["set"] == "pooled_tok"])
    others = [
        r
        for r in out
        if r["set"] == "snap" and r["candidate"] and int(r["cell"][1:].split("s")[0]) not in SNAP
    ]
    if others:
        print("\n== candidates at non-primary seconds (exploratory only) ==")
        show(others)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("scan")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--min-n", type=int, default=100)
    a = ap.parse_args()
    {"scan": cmd_scan, "report": cmd_report}[a.cmd](a)
