"""Drill-down on the late-crossing cells from calibration.py (x600/700/800 crossed in the last minute).

Stress tests:
  A) x (550..900) by crossing time bucket, fill 500 ms
  B) latency 0..2000 ms x fill mode: market (pay ask at match) or IOC limit at a0 + 0/10/20 milli
  C) definitions: first-ever ask crossing (k0), mid crossing (k1), any crossing inside the last minute (k2)
  D) spread at crossing; E) size shown at fill and capacity; F) June vs July
June-July had a 250 ms exchange taker hold plus ~130 ms Tokyo->London order path and ~120 ms book
delivery, so ~500 ms is the realistic latency for that period (today's hold is reportedly 50 ms).
"""

import argparse
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from calibration import boot, fee
from same_token_wf2 import GAP_US, MKT_US, SIZE, done_days, markets, settlements

XS = (550, 600, 650, 700, 750, 800, 850, 900)
LATS = (0, 250, 500, 750, 1000, 2000)  # ms
TB = (0, 120, 180, 240, 260, 280, 290, 295, 300)  # crossing time buckets, s
LATE_US = 240_000_000
EP, TOK, X, KIND, T, A0, B0, SZ0, WON = range(9)
NL = len(LATS)


def lat_cols(i):
    return (
        9 + 3 * i,
        10 + 3 * i,
        11 + 3 * i,
    )  # ask, size, status (0 ok, 2 invalid, 3 small, 4 after close)


def records(m, W):
    t = m["t"]
    if len(t) < 2 or W is None:
        return np.zeros((0, 8 + 3 * NL))
    b, a, sz = m["bid"].astype(np.int64), m["ask"].astype(np.int64), m["asz"]
    v = (b >= 0).all(1) & (a > 0).all(1) & (a[:, 0] == 1000 - b[:, 1]) & (a[:, 1] == 1000 - b[:, 0])
    vi = np.flatnonzero(v)
    late = t[vi] >= LATE_US
    out = []
    for k in (0, 1):
        ak, mk = a[vi, k], (a[vi, k] + b[vi, k]) / 2
        for kind, s, base in ((0, ak, 0), (1, mk, 0), (2, ak[late], int((~late).sum()))):
            for x in XS:
                lo = np.flatnonzero(s < x)
                if not len(lo):
                    continue
                hi = np.flatnonzero(s[lo[0] :] >= x)
                if not len(hi):
                    continue
                j = vi[base + lo[0] + hi[0]]
                row = [k, x, kind, t[j], a[j, k], b[j, k], sz[j, k], int(W == k)]
                for L in LATS:
                    te = t[j] + L * 1000
                    if te >= MKT_US:
                        row += [-1, 0, 4]
                        continue
                    xx = int(np.searchsorted(t, te, "right")) - 1
                    if not v[xx] or te - t[xx] > GAP_US:
                        row += [-1, 0, 2]
                    else:
                        row += [a[xx, k], sz[xx, k], 0 if sz[xx, k] >= SIZE else 3]
                out.append(row)
    return np.array(out, float).reshape(-1, 8 + 3 * NL)


def _day(args):
    ddir, out, sett = args
    R = [
        np.c_[np.full(len(r), e), r] for e, m in markets(ddir) if len(r := records(m, sett.get(e)))
    ]
    if R:
        np.savez_compressed(out / f"{ddir.name}.npz", r=np.concatenate(R))
    return ddir.name, len(R)


def cmd_scan(a):
    a.out.mkdir(parents=True, exist_ok=True)
    sett = settlements(a.cache)
    days = done_days(a.cache)[: a.days or None]
    jobs = [(d, a.out, sett) for d in days if a.force or not (a.out / f"{d.name}.npz").exists()]
    with Pool(a.workers) as p:
        for d, k in p.imap_unordered(_day, jobs):
            print(d, k, flush=True)


# ---------------------------------------------------------------- report


def stat(R, D, nd, sel, li, lim=None):
    """Net per attempt and per fill at latency index li. lim: None = market, else IOC limit offset (milli)."""
    ca, _, cst = lat_cols(li)
    r, d = R[sel], D[sel]
    if not len(r):
        return None
    ok = r[:, cst] == 0
    if lim is not None:
        ok &= r[:, ca] <= r[:, A0] + lim
    a1 = r[:, ca]
    net = np.where(ok, 1000 * r[:, WON] - a1 - fee(a1), 0.0)
    S = np.bincount(d, net, nd)[:, None]
    Nf = np.bincount(d, ok, nd)[:, None]
    lo, hi = boot(S, Nf)[:, 0] / 10
    nf = int(ok.sum())
    return {
        "n": len(r),
        "fill": nf / len(r),
        "ask": a1[ok].mean() / 10 if nf else np.nan,
        "win": 100 * r[ok, WON].mean() if nf else np.nan,
        "net": net.sum() / max(nf, 1) / 10,
        "ci": (lo, hi),
        "per_att": net.sum() / len(r) / 10,
        "usd_day5": net.sum() * SIZE / 1000 / nd,
    }


def line(lab, s):
    if s is None:
        return f"{lab:>34}  -"
    return (
        f"{lab:>34} n {s['n']:>6} fill {s['fill']:5.2f} ask {s['ask']:6.2f} win {s['win']:6.2f} "
        f"net/fill {s['net']:6.2f} [{s['ci'][0]:6.2f},{s['ci'][1]:6.2f}] per-att {s['per_att']:6.2f} $/day@5 {s['usd_day5']:6.2f}"
    )


def cmd_report(a):
    fs = sorted(a.out.glob("????-??-??.npz"))
    days = [f.stem for f in fs]
    Rs = [np.load(f)["r"] for f in fs]
    R = np.concatenate(Rs)
    D = np.concatenate([np.full(len(r), i) for i, r in enumerate(Rs)]).astype(int)
    nd = len(days)
    L500 = LATS.index(500)
    tsec = R[:, T] / 1e6
    tb = np.searchsorted(TB, tsec, "right") - 1
    late = tsec >= 240
    k0 = R[:, KIND] == 0
    dd = np.array(days)
    print(
        f"{nd} days {days[0]}..{days[-1]}, {len(R)} crossing records. Units c/share; CI 90% day bootstrap, all days."
    )
    print("Holdout already seen in calibration.py: these are explanatory cuts, not a fresh test.\n")

    print("== A) first-ever ask crossing (k0), fill at 500 ms market, by x and crossing time ==")
    for x in XS:
        for i in range(len(TB) - 1):
            sel = k0 & (R[:, X] == x) & (tb == i)
            if sel.sum() >= 50:
                print(line(f"x{x} t{TB[i]}-{TB[i + 1]}s", stat(R, D, nd, sel, L500)))
        print()

    print("== B) k0, crossing in last minute: latency x fill mode ==")
    for x in (600, 700, 800):
        sel = k0 & late & (R[:, X] == x)
        for li, L in enumerate(LATS):
            for lim, nm in ((None, "mkt"), (0, "ioc+0"), (10, "ioc+1c"), (20, "ioc+2c")):
                print(line(f"x{x} {L}ms {nm}", stat(R, D, nd, sel, li, lim)))
        print()

    print("== C) definitions, last minute, 500 ms market ==")
    for kind, nm in (
        (0, "k0 first-ever ask"),
        (1, "k1 first-ever mid"),
        (2, "k2 any ask cross >=240s"),
    ):
        for x in XS:
            sel = (R[:, KIND] == kind) & late & (R[:, X] == x)
            print(line(f"{nm} x{x}", stat(R, D, nd, sel, L500)))
        print()

    print("== D) k0 last minute, 500 ms market, by spread at crossing (milli) ==")
    spr = R[:, A0] - R[:, B0]
    for x in (600, 700, 800):
        for lo_, hi_ in ((0, 10), (11, 30), (31, 1000)):
            sel = k0 & late & (R[:, X] == x) & (spr >= lo_) & (spr <= hi_)
            print(line(f"x{x} spread {lo_}-{hi_}", stat(R, D, nd, sel, L500)))
        print()

    print("== E) k0 last minute, 500 ms: size shown at fill (shares) and top-of-book capacity ==")
    ca, cs, cst = lat_cols(L500)
    for x in (600, 700, 800):
        sel = k0 & late & (R[:, X] == x) & (R[:, cst] == 0)
        if not sel.any():
            continue
        s = R[sel, cs]
        a1 = R[sel, ca]
        net = 1000 * R[sel, WON] - a1 - fee(a1)
        cap = [float((np.minimum(s, q) * net).sum() / 1000 / nd) for q in (5, 25, 100)]
        print(
            f"  x{x}: size p10/p50/p90 {np.percentile(s, [10, 50, 90]).round(0)}  "
            f"$/day at min(size, 5/25/100) shares {np.round(cap, 2)}  events/day {sel.sum() / nd:.1f}"
        )
    print()

    print("== F) k0 last minute, 500 ms market: by month and pre/holdout split ==")
    for x in (600, 700, 800):
        for nm, msk in (
            ("Jun", np.char.startswith(dd, "2026-06")),
            ("Jul", np.char.startswith(dd, "2026-07")),
            ("pre", dd < "2026-07-19"),
            ("hold", dd >= "2026-07-19"),
        ):
            sel = k0 & late & (R[:, X] == x) & msk[D]
            print(line(f"x{x} {nm}", stat(R, D, nd, sel, L500)))
        print()

    print(
        "== G) one trade per market: first k0 crossing of any x in 600..800 after 240 s, 500 ms market =="
    )
    sel = k0 & late & np.isin(R[:, X], (600, 650, 700, 750, 800))
    idx = np.flatnonzero(sel)
    o = np.lexsort((R[idx, T], R[idx, EP]))
    idx = idx[o]
    first = idx[np.r_[True, R[idx[1:], EP] != R[idx[:-1], EP]]]
    m = np.zeros(len(R), bool)
    m[first] = True
    print(line("first late crossing per market", stat(R, D, nd, m, L500)))
    print(line("  same, IOC limit +1c", stat(R, D, nd, m, L500, 10)))


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
    a = ap.parse_args()
    {"scan": cmd_scan, "report": cmd_report}[a.cmd](a)
