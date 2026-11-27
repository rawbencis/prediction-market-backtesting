"""L1: Lighter fast-pull maker on BTC/ETH 15m up/down (frozen/L1_plan.json).

prep DAY..      compact one UTC day: Binance USD-M BTC/ETH 10 ms last-price grid + Polymarket 15m UP rows
                (from ~/shadow-rec/DAY or ~/archive/shadow-rec/DAY.tar.zst, extracted to a scratch dir and
                deleted) -> CACHE/DAY.npz
desc DAY..      L1-D: Lighter reprice time after Binance moves; move vs quiet flow and maker markouts
replay DAY..    the maker grid (288 cells per asset) -> CSV + table
v1 / v2         validation (synthetic; reproduction of lighter_flips fair3 markouts)

Exchange-time join; views: Lighter + 2 ms, Binance + 4 ms, Polymarket + 116 ms.
"""

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOME = Path.home()
LDATA = HOME / "lighter-data"
SREC = HOME / "shadow-rec"
SARC = HOME / "archive" / "shadow-rec"
CACHE = HOME / "pascal-data" / "l1cache"
SCRATCH = HOME / "pascal-data" / "l1scratch"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

LB_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4")]
                 + [(f"{s}{i}{f}", t) for s in "bk" for i in (1, 2, 3) for f, t in (("", "<u2"), ("s", "<f4"))])
LT_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("p", "<u2"), ("s", "<f4"),
                  ("buy", "u1"), ("aa", "<i8"), ("ba", "<i8"), ("id", "<i8")])
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
GRID = 10  # ms
LV_L, LV_B, LV_P = 2.0, 4.0, 116.0
ASSETS = ("btc", "eth")
DOMINANT = 712772
DEV = [f"2026-11-{d:02d}" for d in range(11, 21)]
HOLD = [f"2026-11-{d:02d}" for d in range(21, 28)]


def day_ms(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


# ------------------------------------------------------------------ prep


def _src_dir(day):
    live = SREC / day
    if live.exists() and any(live.glob("bn-*.bin")):
        return live, None
    tar = SARC / f"{day}.tar.zst"
    if not tar.exists():
        raise FileNotFoundError(f"no shadow-rec data for {day}")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = SCRATCH / day
    if out.exists():
        shutil.rmtree(out)
    subprocess.run(["tar", "--zstd", "-xf", str(tar), "-C", str(SCRATCH), "--wildcards", f"{day}/bn-*", f"{day}/pm-*"],
                   check=True)
    return out, out


def prep(day):
    CACHE.mkdir(parents=True, exist_ok=True)
    src, tmp = _src_dir(day)
    try:
        d0 = day_ms(day)
        n = 86400 * 1000 // GRID
        grids = {}
        for ai in (0, 1):
            g = np.full(n, np.nan, dtype=np.float64)
            grids[ai] = g
        last_t = {0: [], 1: []}
        last_p = {0: [], 1: []}
        # include the previous day's last hour so the grid starts filled (optional: skipped if absent)
        for f in sorted(src.glob("bn-*.bin")):
            r = np.fromfile(f, dtype=BN_DT, count=f.stat().st_size // BN_DT.itemsize)
            r = r[(r["src"] == 1) & (r["a"] <= 1)]
            for ai in (0, 1):
                q = r[r["a"] == ai]
                last_t[ai].append(q["T"])
                last_p[ai].append(q["p"])
        out = {}
        for ai in (0, 1):
            t = np.concatenate(last_t[ai]) if last_t[ai] else np.zeros(0, np.int64)
            p = np.concatenate(last_p[ai]) if last_p[ai] else np.zeros(0)
            o = np.argsort(t, kind="stable")
            t, p = t[o], p[o]
            gt = d0 + (np.arange(n, dtype=np.int64) + 1) * GRID  # grid point k = last price with T <= d0+(k+1)*10
            i = np.searchsorted(t, gt, side="right") - 1
            g = np.where(i >= 0, p[np.maximum(i, 0)], np.nan)
            out[f"bn{ai}"] = g.astype(np.float32)
            out[f"bn_n{ai}"] = np.array([len(t)])
        rows = []
        for f in sorted(src.glob("pm-*.bin")):
            r = np.fromfile(f, dtype=PM_DT, count=f.stat().st_size // PM_DT.itemsize)
            rows.append(r[(r["tf"] == 15) & (r["a"] <= 1)])
        pm = np.concatenate(rows) if rows else np.zeros(0, PM_DT)
        out["pm"] = pm
        np.savez_compressed(CACHE / f"{day}.npz", **out)
        return {k: (len(v) if hasattr(v, "__len__") else v) for k, v in out.items()}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ loading


class Day:
    def __init__(self, day):
        self.day = day
        self.d0 = day_ms(day)
        z = np.load(CACHE / f"{day}.npz")
        self.bn = {0: z["bn0"].astype(np.float64), 1: z["bn1"].astype(np.float64)}
        pm = z["pm"]
        self.pm = pm[np.argsort(pm["x"], kind="stable")]
        ld = LDATA / day
        lb = [np.fromfile(f, dtype=LB_DT, count=f.stat().st_size // LB_DT.itemsize) for f in sorted(ld.glob("lb-*.bin"))]
        lt = [np.fromfile(f, dtype=LT_DT, count=f.stat().st_size // LT_DT.itemsize) for f in sorted(ld.glob("lt-*.bin"))]
        self.lb = np.concatenate(lb) if lb else np.zeros(0, LB_DT)
        self.lt = np.concatenate(lt) if lt else np.zeros(0, LT_DT)
        self.lb = self.lb[self.lb["tf"] == 15]
        self.lt = self.lt[self.lt["tf"] == 15]
        self.lt = self.lt[np.argsort(self.lt["T"], kind="stable")]

    def bn_at(self, ai, t_ms):
        """Binance last price at exchange time t (grid)."""
        k = ((np.asarray(t_ms) - self.d0) // GRID - 1).astype(np.int64)
        k = np.clip(k, 0, len(self.bn[ai]) - 1)
        return self.bn[ai][k]

    def windows(self):
        """(asset, epoch) of 15m windows starting this day with Lighter book rows."""
        e0 = self.d0 // 1000
        ks = set(zip(self.lb["a"].tolist(), self.lb["e"].tolist()))
        return sorted((a, e) for a, e in ks if a <= 1 and e0 <= e < e0 + 86400)


def outcomes():
    """market outcome per (asset, epoch): 1.0 Up, 0.0 Down (Lighter, else Polymarket winner)."""
    mk = {}
    with open(LDATA / "lighter_markets.csv") as f:
        for r in csv.reader(f):
            if r and r[0] in ASSETS and r[1] == "15":
                mk[r[3]] = (ASSETS.index(r[0]), int(r[2]))
    out = {}
    p = LDATA / "lighter_outcomes.csv"
    if p.exists():
        with open(p) as f:
            for r in csv.reader(f):
                if len(r) >= 3 and r[0] in mk and r[2] in ("1", "2"):
                    out[mk[r[0]]] = 1.0 if r[2] == "2" else 0.0
    p = LDATA / "pm_winners.csv"
    if p.exists():
        with open(p) as f:
            for r in csv.reader(f):
                try:
                    a, e, w = r[0], int(r[1]), r[2]
                except (ValueError, IndexError):
                    continue
                if a in ASSETS:
                    out.setdefault((ASSETS.index(a), e), 1.0 if w.lower() in ("up", "1", "1.0") else 0.0)
    return out


# ------------------------------------------------------------------ signals


def move_grid(D, ai, bp, win_ms):
    """bool grid: |p(t)/p(t - win) - 1| >= bp."""
    g = D.bn[ai]
    k = win_ms // GRID
    r = np.full(len(g), np.nan)
    r[k:] = g[k:] / g[:-k] - 1
    with np.errstate(invalid="ignore"):
        return np.abs(r) >= bp * 1e-4


def grid_t(D, k):
    return D.d0 + (np.asarray(k) + 1) * GRID


def pm_rows(D, ai, e):
    m = D.pm[(D.pm["a"] == ai) & (D.pm["e"] == e)]
    ok = (m["b"] > 0) & (m["k"] > m["b"])
    return m[ok]


def pm_mid_at(P, t):
    """Polymarket UP mid at exchange time t (vector)."""
    if len(P) == 0:
        return np.full(np.shape(t), np.nan)
    j = np.searchsorted(P["x"], t, side="right") - 1
    mid = (P["b"][np.maximum(j, 0)] + P["k"][np.maximum(j, 0)]) / 2e4
    return np.where(j >= 0, mid, np.nan)


# ------------------------------------------------------------------ L1-D command


def q3(v):
    v = np.asarray([x for x in v if x == x])
    if not len(v):
        return "-"
    return " / ".join(f"{np.quantile(v, q):.0f}" for q in (0.1, 0.5, 0.9))


def cmd_desc(days, rest):
    days = days or DEV
    assert not set(days) & set(HOLD), "L1-D runs on dev days only"
    rep = defaultdict(list)  # asset -> reprice ms
    rows = []  # per trade: asset, moved, size, notional, taker, maker, mo1, mo5, mo30, day
    for day in days:
        D = Day(day)
        for ai in (0, 1):
            mv = move_grid(D, ai, 2, 250)
            on = np.flatnonzero(mv & ~np.r_[False, mv[:-1]])
            t_on = grid_t(D, on)
            L = D.lb[D.lb["a"] == ai]
            for e in np.unique(L["e"]):
                Le = L[L["e"] == e]
                e = int(e)
                Le = Le[np.argsort(Le["x"], kind="stable")]
                x = Le["x"].astype(np.int64)
                sel = t_on[(t_on >= e * 1000 - 7_200_000) & (t_on < e * 1000 + 898_000)]
                # only the window that is current at the onset (the one traded most)
                sel = sel[(sel >= e * 1000) & (sel < e * 1000 + 900_000)]
                if not len(sel) or len(Le) < 2:
                    continue
                j0 = np.searchsorted(x, sel, side="right") - 1
                b, k = Le["b1"], Le["k1"]
                chg = np.r_[True, (b[1:] != b[:-1]) | (k[1:] != k[:-1])]
                cidx = np.flatnonzero(chg)
                for t0, j in zip(sel, j0):
                    if j < 0 or b[j] == 0 or k[j] == 0:
                        continue
                    c = cidx[np.searchsorted(cidx, j, side="right"):]
                    dt = float(x[c[0]] - t0) if len(c) and x[c[0]] - t0 < 5000 else np.nan
                    rep[ai].append(dt)
            T = D.lt[D.lt["a"] == ai]
            if not len(T):
                continue
            cs = np.r_[0, np.cumsum(mv)]
            tt = T["T"].astype(np.int64)
            k_hi = np.clip((tt - D.d0) // GRID, 0, len(mv))
            k_lo = np.clip((tt - 500 - D.d0) // GRID, 0, len(mv))
            moved = (cs[k_hi] - cs[k_lo]) > 0
            p = T["p"] / 1e4
            s = T["s"].astype(float)
            buy = T["buy"].astype(bool)
            sgn = np.where(buy, 1.0, -1.0)
            maker = np.where(buy, T["aa"], T["ba"])
            taker = np.where(buy, T["ba"], T["aa"])
            mos = np.full((len(T), 3), np.nan)
            for e in np.unique(T["e"]):
                ii = np.flatnonzero(T["e"] == e)
                P = pm_rows(D, ai, int(e))
                for c, dt in enumerate((1000, 5000, 30000)):
                    mos[ii, c] = sgn[ii] * (p[ii] - pm_mid_at(P, tt[ii] + dt))
            for i in range(len(T)):
                rows.append((ai, bool(moved[i]), s[i], s[i] * p[i], int(taker[i]), int(maker[i]), *mos[i], day))
        print(f"[desc] {day}: trades {len(rows)}", flush=True)
    out = []
    P = out.append
    P("### L1-D (dev 11-11..11-20)\n")
    P("Lighter reprice after a Binance USD-M move >= 2 bp / 250 ms (onset, exchange time) — time to the next change "
      "of Lighter's best bid or ask, ms (p10 / p50 / p90); NaN = no change within 5 s:\n")
    P("| asset | onsets | changed within 5 s | p10 / p50 / p90 ms | share < 50 ms | share 250-400 ms |")
    P("|---|---:|---:|---|---:|---:|")
    for ai in (0, 1):
        v = np.array(rep[ai])
        ok = v[v == v]
        P(f"| {ASSETS[ai]} | {len(v)} | {len(ok)} | {q3(ok)} | {np.mean(ok < 50) if len(ok) else 0:.0%} | "
          f"{np.mean((ok >= 250) & (ok < 400)) if len(ok) else 0:.0%} |")
    R = rows
    P("\nTaker flow, move (within 500 ms after a Binance >= 2 bp / 250 ms move) vs quiet; maker markout vs Polymarket "
      "UP mid (exchange time), ¢/share, size-weighted:\n")
    P("| asset | set | trades | shares | share of shares | maker mo 1 s | maker mo 5 s | maker mo 30 s | n with 5 s mid |")
    P("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    verdict = {}
    for ai in (0, 1):
        tot = sum(r[2] for r in R if r[0] == ai)
        for name, flag in (("move", True), ("quiet", False)):
            rs = [r for r in R if r[0] == ai and r[1] == flag]
            sh = sum(r[2] for r in rs)
            ms = []
            for c in (6, 7, 8):
                w = [(r[2], r[c]) for r in rs if r[c] == r[c]]
                ms.append(100 * sum(a * b for a, b in w) / max(sum(a for a, _ in w), 1e-9) if w else math.nan)
            n5 = sum(1 for r in rs if r[7] == r[7])
            P(f"| {ASSETS[ai]} | {name} | {len(rs)} | {sh:,.0f} | {sh / max(tot, 1e-9):.0%} | {ms[0]:.2f} | "
              f"{ms[1]:.2f} | {ms[2]:.2f} | {n5} |")
            if not flag:
                verdict[ASSETS[ai]] = ms[1]
    # by taker account (top 6 by shares, others pooled), quiet and move separately
    P("\nBy taker account (top 6 by shares across both assets; maker markout 5 s, ¢/share):\n")
    by = defaultdict(float)
    for r in R:
        by[r[4]] += r[2]
    top = [a for a, _ in sorted(by.items(), key=lambda kv: -kv[1])[:6]]
    P("| taker | shares | share | quiet share of its volume | maker mo 5 s (quiet) | maker mo 5 s (move) | maker mo 30 s (all) |")
    P("|---|---:|---:|---:|---:|---:|---:|")
    allsh = sum(by.values())
    for acc in top + ["others"]:
        rs = [r for r in R if (r[4] == acc if acc != "others" else r[4] not in top)]
        sh = sum(r[2] for r in rs)

        def wm(sub, c):
            w = [(r[2], r[c]) for r in sub if r[c] == r[c]]
            return 100 * sum(a * b for a, b in w) / max(sum(a for a, _ in w), 1e-9) if w else math.nan

        qs = [r for r in rs if not r[1]]
        P(f"| {acc} | {sh:,.0f} | {sh / max(allsh, 1e-9):.0%} | {sum(r[2] for r in qs) / max(sh, 1e-9):.0%} | "
          f"{wm(qs, 7):.2f} | {wm([r for r in rs if r[1]], 7):.2f} | {wm(rs, 8):.2f} |")
    mk = defaultdict(float)
    for r in R:
        mk[r[5]] += r[2]
    P("\nTop makers by shares: " + ", ".join(f"{a} {s / max(allsh, 1e-9):.0%}" for a, s in
                                             sorted(mk.items(), key=lambda kv: -kv[1])[:5]))
    stop = all(v <= 0 for v in verdict.values() if v == v)
    P(f"\n**Stop rule (quiet-time maker markout at 5 s > 0?):** " +
      ", ".join(f"{k} {v:+.2f}¢" for k, v in verdict.items()) +
      (" -> **no asset positive: L1 is closed.**" if stop else " -> at least one asset positive: continue to the replay."))
    md = "\n".join(out)
    Path(REPO / "reports" / "tokyo" / "l1").mkdir(parents=True, exist_ok=True)
    (REPO / "reports" / "tokyo" / "l1" / "l1_desc.md").write_text(md + "\n")
    print(md)
    return 0



# ------------------------------------------------------------------ replay engine

PULLS = {"none": None, "1bp/250ms": (1, 250), "2bp/250ms": (2, 250), "2bp/1s": (2, 1000), "4bp/1s": (4, 1000)}
HS = (0.02, 0.03, 0.04, 0.05)
FAIRS = ("pm_mid", "lighter_mid")
REPOSTS = (1, 5)
DS = (0, 300)
STOPS = (True, False)
SIZES = (30.0, 5.0)
CAP = 150.0
MOVE = 0.01


def cell_list():
    out = []
    for fair in FAIRS:
        for h in HS:
            for pull in PULLS:
                for rq in ((None,) if pull == "none" else REPOSTS):
                    for D in DS:
                        for stop in STOPS:
                            out.append((fair, h, pull, rq, D, stop))
    return out


def cell_name(c):
    fair, h, pull, rq, D, stop = c
    return f"{fair}|h{int(round(h * 100))}|{pull}" + (f"|q{rq}" if rq else "") + f"|D{D}|{'stop60' if stop else 'to_close'}"


class Win:
    """One (asset, window): arrays in exchange ms.
    L: (x, bid, ask) Lighter best; P: (x, bid, ask) PM UP best; T: (t, p, s, buy, taker, maker);
    sig: {pull: sorted signal grid times (exchange ms)}; W outcome; e epoch s."""

    def __init__(self, ai, e, L, P, T, sig, W, fee_rate=0.07):
        self.ai, self.e, self.L, self.P, self.T, self.sig, self.W = ai, e, L, P, T, sig, W
        self.t0, self.t1 = e * 1000, e * 1000 + 900_000
        self.fee_rate = fee_rate
        self._fair = {}

    def fair_decisions(self, fair):
        """Requote decisions for one fair source: (decision time seen, quoted fair)."""
        if fair in self._fair:
            return self._fair[fair]
        if fair == "pm_mid":
            x, b, k = self.P
            lag = LV_P
        else:
            x, b, k = self.L
            lag = LV_L
        ok = (b > 0) & (k > b)
        x, m = x[ok] + lag, ((b + k) / 2)[ok]
        if len(m):
            keep = np.r_[True, m[1:] != m[:-1]]
            x, m = x[keep], m[keep]
        dt, qf = [], []
        cur = None
        for t, v in zip(x.tolist(), m.tolist()):
            if cur is None or abs(v - cur) >= MOVE - 1e-12:
                cur = v
                dt.append(t)
                qf.append(v)
        r = (np.array(dt, dtype=np.float64), np.array(qf))
        self._fair[fair] = r
        self._fair["raw_" + fair] = (x, m)
        return r

    def fair_seen(self, fair, t):
        self.fair_decisions(fair)
        x, m = self._fair["raw_" + fair]
        if not len(x):
            return np.full(len(t), np.nan)
        i = np.searchsorted(x, t, side="right") - 1
        return np.where(i >= 0, m[np.maximum(i, 0)], np.nan)


def off_intervals(sig_t, D, quiet_s):
    """Signal grid times (exchange) -> pulled intervals [start, end): cancel lands seen + 2 + D, re-post lands
    2 ms after `quiet` s with no further signal."""
    if sig_t is None or not len(sig_t):
        return np.zeros(0), np.zeros(0)
    seen = sig_t + LV_B
    q = quiet_s * 1000.0
    brk = np.r_[True, np.diff(seen) >= q]
    starts = seen[brk]
    ends_idx = np.r_[np.flatnonzero(brk)[1:] - 1, len(seen) - 1]
    ends = seen[ends_idx] + q + 2.0
    return starts + 2.0 + D, ends


def in_intervals(t, s, e):
    if not len(s):
        return np.zeros(len(t), bool)
    i = np.searchsorted(s, t, side="right") - 1
    return (i >= 0) & (t < e[np.maximum(i, 0)])


def simulate(w, c, Q, stats=None):
    """-> list of fills (t, side +1 long Up / -1 short Up, q, shares, taker, fair_seen) for one window/cell."""
    fair, h, pull, rq, D, stop = c
    tT, tp, ts, tb, tk = w.T[0], w.T[1], w.T[2], w.T[3], w.T[4]
    t_end = w.t1 - (60_000 if stop else 0)
    sel = (tT >= w.t0) & (tT < t_end)
    dts, qfs = w.fair_decisions(fair)
    if not len(dts) or not sel.any():
        if stats is not None:
            stats["msgs"] += 0
        return []
    land = dts + 2.0 + D
    first_land = dts[0] + 2.0  # the first post is a new order: + 2 ms
    land[0] = first_land
    bids = np.floor((qfs - h) * 100 + 1e-9) / 100
    asks = np.ceil((qfs + h) * 100 - 1e-9) / 100
    # post-only at landing: Lighter book as of landing
    lx, lb_, lk_ = w.L
    j = np.searchsorted(lx + 0.0, land - 0.0, side="right") - 1
    jj = np.maximum(j, 0)
    bid_ok = (j >= 0) & (bids > 0) & ~((lk_[jj] > 0) & (bids >= lk_[jj] - 1e-9))
    ask_ok = (j >= 0) & (asks < 1) & ~((lb_[jj] > 0) & (asks <= lb_[jj] + 1e-9))
    # pulled intervals
    sg = w.sig.get(pull) if pull != "none" else None
    os_, oe_ = off_intervals(sg, D, rq or 1)
    idx = np.flatnonzero(sel)
    t = tT[idx]
    d = np.searchsorted(land, t, side="right") - 1
    live = (d >= 0) & ~in_intervals(t, os_, oe_)
    dd = np.maximum(d, 0)
    buy = tb[idx] > 0
    q = np.where(buy, asks[dd], bids[dd])
    okside = np.where(buy, ask_ok[dd], bid_ok[dd])
    # Lighter best just before the trade (book without us)
    jb = np.searchsorted(lx, t, side="left") - 1
    jbb = np.maximum(jb, 0)
    bb = np.where(jb >= 0, lb_[jbb], 0.0)
    ba = np.where(jb >= 0, lk_[jbb], 0.0)
    p = tp[idx]
    improving = np.where(buy, (ba <= 0) | (q < ba - 1e-9), (bb <= 0) | (q > bb + 1e-9))
    hit = np.where(buy, np.where(improving, p >= q - 1e-9, p > q + 1e-9),
                   np.where(improving, p <= q + 1e-9, p < q - 1e-9))
    hit &= live & okside
    if stats is not None:
        nd = int(((land >= w.t0) & (land < t_end)).sum())
        stats["msgs"] += 2 * nd + 4 * len(os_)  # a modify per side per requote; cancel+post per side per pull
        stats["secs"] += (t_end - w.t0) / 1000
    out = []
    net = 0.0
    for i in np.flatnonzero(hit):
        side = -1.0 if buy[i] else 1.0  # taker bought Up from our ask -> we are short Up
        f = min(float(ts[idx[i]]), Q)
        if side > 0:
            f = min(f, CAP - net)
        else:
            f = min(f, CAP + net)
        if f <= 1e-9:
            continue
        net += side * f
        out.append((float(t[i]), side, float(q[i]), f, int(tk[idx[i]]), float(w.fair_seen(fair, np.array([t[i]]))[0])))
    return out


def fill_metrics(w, fills):
    """-> per-fill metrics arrays: shares, settle pnl, vs fair, mo1/5/30 vs PM mid, hedged pnl (300 ms)."""
    if not fills:
        return None
    F = np.array([(f[0], f[1], f[2], f[3], f[5]) for f in fills])
    t, sd, q, s, fr = F.T
    x, b, k = w.P
    ok = (b > 0) & (k > b)
    x, b, k = x[ok], b[ok], k[ok]
    mid = (b + k) / 2

    def m_at(tt):
        if not len(x):
            return np.full(len(tt), np.nan)
        i = np.searchsorted(x, tt, side="right") - 1
        return np.where(i >= 0, mid[np.maximum(i, 0)], np.nan)

    res = {"s": s, "settle": sd * (w.W - q) * s if w.W == w.W else np.full(len(s), np.nan),
           "vsfair": sd * (fr - q) * s}
    for key, dt in (("mo1", 1000), ("mo5", 5000), ("mo30", 30000)):
        res[key] = sd * (m_at(t + dt) - q) * s
    # Polymarket taker hedge at fill + 2 ms (seen) + 300 ms
    if len(x):
        i = np.searchsorted(x, t + LV_L + 300, side="right") - 1
        ii = np.maximum(i, 0)
        hb, hk = b[ii], k[ii]
        r = w.fee_rate
        # long Up on Lighter -> buy PM Down at 1 - bid; short Up -> buy PM Up at ask
        hc = np.where(sd > 0, (1 - hb) + r * hb * (1 - hb), hk + r * hk * (1 - hk))
        pc = np.where(sd > 0, q, 1 - q)
        res["hedged"] = np.where(i >= 0, (1 - pc - hc) * s, np.nan)
    else:
        res["hedged"] = np.full(len(s), np.nan)
    return res


def build_windows(day, census=None, outs=None):
    from lp_arb import fees as fz

    D = Day(day)
    outs = outs if outs is not None else outcomes()
    census = census if census is not None else fz.load_census()
    sigs = {}
    for ai in (0, 1):
        sigs[ai] = {}
        for name, spec in PULLS.items():
            if spec is None:
                continue
            mv = move_grid(D, ai, spec[0], spec[1])
            sigs[ai][name] = grid_t(D, np.flatnonzero(mv)).astype(np.float64)
    ws = []
    for ai, e in D.windows():
        L = D.lb[(D.lb["a"] == ai) & (D.lb["e"] == e)]
        L = L[np.argsort(L["x"], kind="stable")]
        lb_, lk_ = L["b1"] / 1e4, L["k1"] / 1e4
        Lt = (L["x"].astype(np.float64), lb_, lk_)
        P = D.pm[(D.pm["a"] == ai) & (D.pm["e"] == e)]
        Pt = (P["x"].astype(np.float64), P["b"] / 1e4, P["k"] / 1e4)
        T = D.lt[(D.lt["a"] == ai) & (D.lt["e"] == e)]
        buy = T["buy"].astype(bool)
        Tt = (T["T"].astype(np.float64), T["p"] / 1e4, T["s"].astype(np.float64), np.where(buy, 1.0, -1.0),
              np.where(buy, T["ba"], T["aa"]).astype(np.int64))
        W = outs.get((ai, int(e)), np.nan)
        rate, _ = fz.rate_for(ASSETS[ai], int(e), census)
        lo, hi = int(e) * 1000.0 - 10_000, int(e) * 1000.0 + 900_000
        sw = {}
        for name, sg in sigs[ai].items():
            a_, b_ = np.searchsorted(sg, [lo, hi])
            sw[name] = sg[a_:b_].copy()
        ws.append(Win(ai, int(e), Lt, Pt, Tt, sw, W, rate))
    return ws


METRICS = ("fills", "s", "settle", "vsfair", "mo1", "mo5", "mo30", "hedged", "top3", "msgs", "secs")


def run_cells(ws, cells, sizes=SIZES):
    """-> {(cell, Q): array (n_windows, len(METRICS))}, plus taker shares for top-3 shares."""
    out = {}
    for Q in sizes:
        for c in cells:
            A = np.zeros((len(ws), len(METRICS)))
            for wi, w in enumerate(ws):
                st = {"msgs": 0, "secs": 0.0}
                fl = simulate(w, c, Q, st)
                A[wi, METRICS.index("msgs")] = st["msgs"]
                A[wi, METRICS.index("secs")] = st["secs"]
                m = fill_metrics(w, fl)
                if m is None:
                    continue
                A[wi, 0] = len(fl)
                for k in ("s", "settle", "vsfair", "mo1", "mo5", "mo30", "hedged"):
                    A[wi, METRICS.index(k)] = np.nansum(m[k])
                # taker ids stored for the top-3 share below
                A[wi, METRICS.index("top3")] = 0
                out.setdefault(("takers", cell_name(c), Q), []).extend((f[4], f[3]) for f in fl)
            out[(cell_name(c), Q)] = A
    return out


def cell_table(res, ws, cells, Q, ndays, days):
    rows = []
    wday = np.array([datetime.fromtimestamp(w.e, timezone.utc).strftime("%Y-%m-%d") for w in ws])
    rng = np.random.default_rng(11)
    nb = 1000
    idx = rng.integers(0, len(ws), size=(nb, len(ws))) if len(ws) else None
    for c in cells:
        nm = cell_name(c)
        A = res[(nm, Q)]
        S = A.sum(axis=0)
        s = S[1]
        tk = res.get(("takers", nm, Q), [])
        agg = defaultdict(float)
        for a, f in tk:
            agg[a] += f
        top3 = sum(sorted(agg.values(), reverse=True)[:3]) / max(sum(agg.values()), 1e-9) if agg else math.nan
        if s > 0 and idx is not None:
            bs = A[idx]  # nb x nw x m
            num, den = bs[:, :, 2].sum(axis=1), bs[:, :, 1].sum(axis=1)
            est = 100 * num / np.maximum(den, 1e-9)
            lo, hi = np.quantile(est, [0.05, 0.95])
        else:
            lo = hi = math.nan
        dpos = sum(1 for d in days if A[wday == d, 2].sum() > 0)
        rows.append(dict(cell=nm, fair=c[0], h=c[1], pull=c[2], repost=c[3], D=c[4], stop60=c[5], Q=Q,
                         fills_day=S[0] / ndays, shares_day=s / ndays,
                         vsfair=100 * S[3] / s if s else math.nan, mo1=100 * S[4] / s if s else math.nan,
                         mo5=100 * S[5] / s if s else math.nan, mo30=100 * S[6] / s if s else math.nan,
                         settle=100 * S[2] / s if s else math.nan, usd_day=S[2] / ndays, ci_lo=lo, ci_hi=hi,
                         days_pos=dpos, msgs_per_s=S[9] / max(S[10], 1e-9), top3=top3,
                         hedged=100 * S[7] / s if s else math.nan))
    return rows


def cmd_replay(days, rest):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--asset", default="both")
    a = ap.parse_args(rest)
    if a.tag == "holdout":
        assert (REPO / "frozen" / "L1_pick.json").exists(), "push frozen/L1_pick.json first"
    else:
        assert not set(days) & set(HOLD), "dev replay must not touch holdout days"
    from lp_arb import fees as fz

    census, outs = fz.load_census(), outcomes()
    cells = cell_list()
    if a.tag == "holdout":
        pk = json.loads((REPO / "frozen" / "L1_pick.json").read_text())
        cells = [tuple(pk["cell_tuple"])]
    allrows = []
    for ai in ((0, 1) if a.asset == "both" else (ASSETS.index(a.asset),)):
        ws = []
        for d in days:
            ws += [w for w in build_windows(d, census, outs) if w.ai == ai]
            print(f"[replay] {ASSETS[ai]} {d}: {len(ws)} windows", flush=True)
        res = run_cells(ws, cells)
        for Q in SIZES:
            for r in cell_table(res, ws, cells, Q, len(days), days):
                r["asset"] = ASSETS[ai]
                allrows.append(r)
    outd = REPO / "reports" / "tokyo" / "l1"
    outd.mkdir(parents=True, exist_ok=True)
    with open(outd / f"l1_{a.tag}_cells.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(allrows[0]))
        wr.writeheader()
        wr.writerows(allrows)
    print(f"wrote {len(allrows)} rows -> {outd / f'l1_{a.tag}_cells.csv'}")
    return 0



# ------------------------------------------------------------------ V1 synthetic


def synth_window(seed, informed=False, n_moves=20, rate_hz=0.2):
    """Martingale PM mid (1¢ steps), Lighter book at mid -/+ 3¢, uninformed takers at Lighter's touch.
    informed: Binance jumps n_moves times; PM mid jumps 2¢ the same way 300 ms later; an informed taker
    arrives 200 ms after each jump and trades 30 shares in the jump's direction at our (stale) quote."""
    rng = np.random.default_rng(seed)
    e = 1_900_000_000 - (1_900_000_000 % 900)
    t0 = e * 1000.0
    # mid path: +-1¢ random steps every ~5 s; informed jumps of 4¢ on PM 300 ms after each Binance jump
    ev = []
    t = t0 - 60_000.0
    while t < t0 + 900_000:
        t += rng.exponential(5000.0)
        ev.append((t, 0.01 * rng.choice([-1.0, 1.0])))
    moves = np.sort(rng.uniform(t0 + 30_000, t0 + 800_000, n_moves)) if informed else np.zeros(0)
    jumps = [(mt + 300.0, float(rng.choice([-1.0, 1.0]))) for mt in moves]
    ev += [(jt, 0.04 * sg) for jt, sg in jumps]
    ev.sort()
    xs, mids, cur = [t0 - 60_000.0], [0.5], 0.5
    for tt_, dm in ev:
        cur = round(min(0.9, max(0.1, cur + dm)), 2)
        xs.append(tt_)
        mids.append(cur)
    xs, mids = np.array(xs), np.array(mids)
    P = (xs, mids - 0.005, mids + 0.005)
    L = (xs.copy(), mids - 0.03, mids + 0.03)
    # uninformed takers
    tt = np.sort(rng.uniform(t0, t0 + 900_000, int(rate_hz * 900)))
    j = np.searchsorted(xs, tt, side="right") - 1
    side = rng.choice([-1.0, 1.0], len(tt))
    p = np.where(side > 0, mids[j] + 0.03, mids[j] - 0.03)
    T = [tt, p, np.full(len(tt), 30.0), side, np.full(len(tt), 1, np.int64)]
    if informed:
        it = moves + 200.0
        ji = np.searchsorted(xs, it, side="right") - 1
        sg = np.array([s for _, s in jumps])
        # the informed taker crosses our quote and the old book: price = old mid + 3¢ * side (Lighter touch)
        ip = np.where(sg > 0, mids[ji] + 0.03, mids[ji] - 0.03)
        T = [np.r_[T[0], it], np.r_[T[1], ip], np.r_[T[2], np.full(len(it), 30.0)], np.r_[T[3], sg],
             np.r_[T[4], np.full(len(it), 2, np.int64)]]
        o = np.argsort(T[0], kind="stable")
        T = [a[o] for a in T]
    sig = {}
    for name in PULLS:
        if PULLS[name] is None:
            continue
        g = np.concatenate([np.arange(mt, mt + PULLS[name][1], GRID) for mt in moves]) if len(moves) else np.zeros(0)
        sig[name] = np.sort(g)
    W = float(mids[-1])  # expected settlement of a martingale (fractional: removes outcome noise)
    return Win(0, e, L, P, tuple(T), sig, W, 0.0)


def cmd_v1(days, rest):
    out = []
    for informed in (False, True):
        ws = [synth_window(s, informed) for s in range(40)]
        cells = [("pm_mid", 0.02, "none", None, D, False) for D in DS] + \
                [("pm_mid", 0.02, "2bp/250ms", 1, D, False) for D in DS]
        res = run_cells(ws, cells, sizes=(30.0,))
        for r in cell_table(res, ws, cells, 30.0, 1, []):
            out.append((informed, r["cell"], r["shares_day"], r["settle"], r["vsfair"], r["usd_day"]))
    lines = ["| takers | cell | shares | ¢/share settle | ¢/share vs fair | $ total |", "|---|---|---:|---:|---:|---:|"]
    for inf, nm, s, st, vf, usd in out:
        lines.append(f"| {'uninformed + informed' if inf else 'uninformed'} | {nm} | {s:.0f} | {st:.2f} | {vf:.2f} | {usd:.2f} |")
    md = "\n".join(lines)
    print(md)
    d = {(inf, nm): usd for inf, nm, _, _, _, usd in out}
    un = [x for x in out if not x[0] and "none" in x[1]]
    checks = {
        "uninformed_pnl_close_to_spread": bool(all(abs(x[3] - 2.0) < 0.5 for x in un)),
        "pull_helps_at_D0": bool(d[(True, "pm_mid|h2|2bp/250ms|q1|D0|to_close")] > d[(True, "pm_mid|h2|none|D0|to_close")]),
        "pull_hurts_at_D300": bool(d[(True, "pm_mid|h2|2bp/250ms|q1|D300|to_close")] < d[(True, "pm_mid|h2|none|D300|to_close")]),
    }
    print(checks)
    (REPO / "reports" / "tokyo" / "l1").mkdir(parents=True, exist_ok=True)
    (REPO / "reports" / "tokyo" / "l1" / "l1_v1.md").write_text(md + "\n\n" + json.dumps(checks) + "\n")
    return 0 if all(checks.values()) else 1



# ------------------------------------------------------------------ V2: reproduce lighter_flips fair3


def cmd_v2(days, rest):
    import lighter_flips as lf

    days = days or DEV
    assert not set(days) & set(HOLD)
    outs = outcomes()
    ref, mine = [], []
    from lp_arb import fees as fz

    census = fz.load_census()
    cont_ws = []
    for day in days:
        D = Day(day)
        for ai, e in D.windows():
            L = D.lb[(D.lb["a"] == ai) & (D.lb["e"] == e)]
            L = L[np.argsort(L["l"], kind="stable")]
            T = D.lt[(D.lt["a"] == ai) & (D.lt["e"] == e)]
            T = T[np.argsort(T["l"], kind="stable")]
            P = D.pm[(D.pm["a"] == ai) & (D.pm["e"] == e)]
            P = P[np.argsort(P["l"], kind="stable")]
            W = outs.get((ai, int(e)), np.nan)
            # (1) the reference code itself
            for r in lf.window(L, T, P, W, int(e)):
                if r["rule"] == "fair3" and r["x"] == 0.70 and r["k"] == 1:
                    ref.append(dict(r, asset=ASSETS[ai], e=int(e)))
            # (2) the same one-shot orders through this engine (exchange clock, this engine's fill rule)
            g = np.arange(int(e) * 1_000_000 + 5_000_000, (int(e) + 900) * 1_000_000, 250_000)
            jp = np.searchsorted(P["l"], g, side="right") - 1
            jpp = np.maximum(jp, 0)
            pmid = (P["b"][jpp] + P["k"][jpp]) / 2e4 if len(P) else np.zeros(len(g))
            pok = (jp >= 0) & (P["b"][jpp] > 0) & (P["k"][jpp] > P["b"][jpp]) if len(P) else np.zeros(len(g), bool)
            Pv = P[(P["b"] > 0) & (P["k"] > P["b"])]
            Pv = Pv[np.argsort(Pv["x"], kind="stable")]
            px_, pm_ = Pv["x"].astype(np.float64), (Pv["b"] + Pv["k"]) / 2e4
            Lx = L[np.argsort(L["x"], kind="stable")]
            lx, lbb, lkk = Lx["x"].astype(np.float64), Lx["b1"] / 1e4, Lx["k1"] / 1e4
            Ts = T[np.argsort(T["T"], kind="stable")]
            tT, tp, tsz, tbuy = Ts["T"].astype(np.float64), Ts["p"] / 1e4, Ts["s"].astype(float), Ts["buy"].astype(bool)

            def mid_x(t):
                i = np.searchsorted(px_, t, side="right") - 1
                return pm_[i] if i >= 0 else np.nan

            for k, gi, fav in lf.flips(g, pmid, pok, 0.70):
                if k != 1:
                    continue
                t0 = (int(g[gi]) + lf.PLACE_LAT) / 1000.0  # ms (their clock is local receive ~ our view)
                if t0 >= (int(e) + 900) * 1000:
                    continue
                for who, side in (("fav", fav), ("dog", 1 - fav)):
                    f0 = mid_x(t0 - LV_P)
                    if f0 != f0:
                        continue
                    fair = f0 if side == 0 else 1 - f0
                    q = math.floor((fair - 0.03) * 100 + 1e-9) / 100
                    j = np.searchsorted(lx, t0, side="right") - 1
                    if j < 0 or not (lkk[j] > lbb[j] > 0):
                        continue
                    bid_s, ask_s = (lbb[j], lkk[j]) if side == 0 else (1 - lkk[j], 1 - lbb[j])
                    if q < 0.01 or q >= ask_s - 1e-9:
                        continue
                    first = q > bid_s + 1e-9
                    for life in lf.LIVES:
                        t_end = min(t0 + life * 1000, (int(e) + 900) * 1000.0)
                        sells = ~tbuy if side == 0 else tbuy
                        pside = tp if side == 0 else 1 - tp
                        thru = (pside <= q + 1e-9) if first else (pside < q - 1e-9)
                        left, mo5, filled = 10.0, 0.0, 0.0
                        for ti in np.flatnonzero(sells & thru & (tT > t0) & (tT <= t_end)):
                            f = min(tsz[ti], left)
                            m5 = mid_x(tT[ti] + 5000)
                            m5 = m5 if side == 0 else 1 - m5
                            mo5 += f * (m5 - q)
                            filled += f
                            left -= f
                            if left <= 1e-9:
                                break
                        mine.append(dict(asset=ASSETS[ai], e=int(e), who=who, life=life, filled=filled, mo5=mo5))
            rate, _ = fz.rate_for(ASSETS[ai], int(e), census)
        cont_ws += [w for w in build_windows(day, census, outs)]
        print(f"[v2] {day}: ref {len(ref)} mine {len(mine)}", flush=True)
    lines = ["| source | life s | orders | filled shares | mo5 ¢/share |", "|---|---:|---:|---:|---:|"]
    for name, rows in (("lighter_flips.window (reference, rerun)", ref), ("this engine, same one-shot orders", mine)):
        for life in (10, 60, 300, 900):
            rs = [r for r in rows if r["life"] == life]
            fs = sum(r["filled"] for r in rs)
            mo = sum((r["mo5"] if r["mo5"] == r["mo5"] else 0) for r in rs)
            lines.append(f"| {name} | {life} | {len(rs)} | {fs:,.0f} | {100 * mo / fs if fs else float('nan'):.2f} |")
    cells = [("pm_mid", 0.03, "none", None, 300, False), ("pm_mid", 0.03, "none", None, 0, False)]
    for ai in (0, 1):
        ws = [w for w in cont_ws if w.ai == ai]
        res = run_cells(ws, cells, sizes=(30.0,))
        for r in cell_table(res, ws, cells, 30.0, len(days), days):
            lines.append(f"| continuous two-sided {ASSETS[ai]} {r['cell']} | - | - | {r['shares_day'] * len(days):,.0f} | {r['mo5']:.2f} |")
    md = "\n".join(lines)
    print(md)
    (REPO / "reports" / "tokyo" / "l1" / "l1_v2.md").write_text(md + "\n")
    return 0



# ------------------------------------------------------------------ dev summary, pick, holdout verdict


def _load_cells(tag):
    import pandas as pd

    return pd.read_csv(REPO / "reports" / "tokyo" / "l1" / f"l1_{tag}_cells.csv")


def _fmt(df, cols):
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            v = r[c]
            vals.append(f"{v:.2f}" if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


COLS = ["asset", "cell", "fills_day", "shares_day", "vsfair", "mo1", "mo5", "mo30", "settle", "ci_lo", "ci_hi",
        "usd_day", "days_pos", "msgs_per_s", "top3", "hedged"]


def cmd_devsum(days, rest):
    df = _load_cells("dev")
    d30 = df[df.Q == 30.0].copy()
    out = ["### L1 dev (11-11..11-20), 30-share quotes\n"]
    d0 = d30[d30.D == 0]
    stop = not (d0.mo5 > 0).any()
    out.append(f"Cells: {len(d30)} (both assets). D = 0 cells with 5 s markout > 0: {(d0.mo5 > 0).sum()} of {len(d0)}. "
               f"D = 300: {(d30[d30.D == 300].mo5 > 0).sum()} of {(d30.D == 300).sum()}.")
    out.append(f"Stop-early rule (no D = 0 cell with 5 s markout > 0): **{'STOP — L1 closed' if stop else 'not triggered'}**.\n")
    elig = d30[(d30.mo5 > 0) & (d30.shares_day >= 300)]
    out.append(f"Eligible for the pick (5 s markout > 0 and >= 300 shares/day): {len(elig)} cells.\n")
    out.append("Top 15 cells by 90% lower bound on ¢/share to settlement (all cells):\n")
    out.append(_fmt(d30.sort_values("ci_lo", ascending=False).head(15), COLS))
    out.append("\nBest cell per (asset, pull, D) by lower bound:\n")
    g = d30.sort_values("ci_lo", ascending=False).groupby(["asset", "pull", "D"], as_index=False).head(1)
    out.append(_fmt(g.sort_values(["asset", "D", "pull"]), COLS))
    out.append("\nSame cells, no pull vs pull, settlement ¢/share (mean over fair, h, stop, repost) by asset and D:\n")
    piv = d30.groupby(["asset", "D", "pull"]).agg(settle=("settle", "mean"), mo5=("mo5", "mean"),
                                                  shares_day=("shares_day", "mean")).reset_index()
    out.append(_fmt(piv, ["asset", "D", "pull", "settle", "mo5", "shares_day"]))
    d5 = df[df.Q == 5.0]
    out.append("\n5-share quotes: top 5 by lower bound:\n")
    out.append(_fmt(d5.sort_values("ci_lo", ascending=False).head(5), COLS))
    pick = None
    if not stop and len(elig):
        pick = elig.sort_values("ci_lo", ascending=False).iloc[0]
        out.append(f"\n**Dev pick:** {pick['asset']} `{pick['cell']}` — settle {pick['settle']:.2f}¢/share "
                   f"(90% CI {pick['ci_lo']:.2f} … {pick['ci_hi']:.2f}), 5 s markout {pick['mo5']:.2f}¢, "
                   f"{pick['shares_day']:.0f} shares/day, ${pick['usd_day']:.2f}/day, days positive {pick['days_pos']}/10.")
    else:
        out.append("\n**Dev pick:** none (" + ("stop-early rule" if stop else "no eligible cell") + ").")
    md = "\n".join(out)
    (REPO / "reports" / "tokyo" / "l1" / "l1_dev_summary.md").write_text(md + "\n")
    print(md)
    if "--write-pick" in rest:
        if pick is None:
            obj = {"name": "L1_pick", "pick": None, "reason": "stop-early" if stop else "no eligible cell"}
        else:
            parts = pick["cell"].split("|")
            rq = int(pick["repost"]) if pick["repost"] == pick["repost"] else None
            ct = [pick["fair"], float(pick["h"]), pick["pull"], rq, int(pick["D"]), bool(pick["stop60"])]
            obj = {"name": "L1_pick", "asset": pick["asset"], "cell": pick["cell"], "cell_tuple": ct,
                   "dev": {k: (float(pick[k]) if isinstance(pick[k], (float, int, np.floating, np.integer)) else str(pick[k]))
                           for k in COLS if k not in ("asset", "cell")},
                   "rule": "frozen/L1_plan.json dev_pick", "holdout": HOLD, "parts": parts,
                   "pass": json.loads((REPO / "frozen" / "L1_plan.json").read_text())["holdout_pass"]}
        (REPO / "frozen" / "L1_pick.json").write_text(json.dumps(obj, indent=1) + "\n")
        print("wrote frozen/L1_pick.json")
    return 0



def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd")
    p.add_argument("days", nargs="*")
    a, rest = p.parse_known_args(argv)
    if a.cmd == "prep":
        for d in a.days:
            if d in HOLD and not (REPO / "frozen" / "L1_pick.json").exists():
                raise SystemExit(f"{d} is a holdout day: push frozen/L1_pick.json first")
            print(d, prep(d), flush=True)
    else:
        return globals()["cmd_" + a.cmd](a.days, rest)


if __name__ == "__main__":
    sys.exit(main())
