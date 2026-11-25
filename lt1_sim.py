"""LT1: favourite ladder with take-profit (frozen plan: frozen/LT1_plan.json). Paper research only.

Per window and token: one ladder post at window start + 1 s (anchor = the token's best bid seen then), `live` latency,
strict maker fills (C port of dsim_strict.maker_strict, lt1_maker.c), pulls on Binance USD-M moves against the side,
cancel at 60 s left; each fill event gets a maker take-profit sell at fill + x; exits E1 (settle) / E2 (taker at 30 s).
Side models are filters applied later (lt1_report.py); this script simulates both tokens.

Usage (one day at a time, capped scope):
  python lt1_sim.py v0 DAY            # C maker == dsim_strict.maker_strict on real orders
  python lt1_sim.py day DAY [tf ...]  # -> out/lt1/lt1-DAY-{tf}m.csv.gz (fill events) + -rungs.csv.gz
"""
import ctypes
import gzip
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / "dublin-retest" / "repo"))
import dsim  # noqa: E402
import f9_live  # noqa: E402,F401  (registers pmlat.SC['live'] and patches olat)
import pmlat  # noqa: E402

OUT = HERE / "out" / "lt1"
SC = pmlat.SC["live"]
BOOK, CXL, HOLD = int(SC["book"]), int(SC["cancel"]), int(SC["hold"])
STEPS, OFFS, KMAX, XS = (1, 2, 3), (0, 1), 5, (1, 2, 3, 5)
PULLS = {"none": None, "b2_250": (2.0, 250), "b4_500": (4.0, 500)}
MODELS = {"pess": 0, "prop": 1}
SIZE, FLOOR = 5.0, 2000
FEES = None

# ------------------------------------------------------------------ C maker

_SO = HERE / "lt1_maker.so"
if not _SO.exists() or _SO.stat().st_mtime < (HERE / "lt1_maker.c").stat().st_mtime:
    subprocess.check_call(["gcc", "-O2", "-shared", "-fPIC", "-o", str(_SO), str(HERE / "lt1_maker.c")])
_lib = ctypes.CDLL(str(_SO))
_P = np.ctypeslib.ndpointer
_lib.lt1_maker.restype = ctypes.c_int
_lib.lt1_maker.argtypes = [_P(np.int64), _P(np.int32), _P(np.int32), _P(np.float32), _P(np.float32), ctypes.c_int64,
                           _P(np.int64), _P(np.uint8), _P(np.int32), _P(np.float64), _P(np.float64), ctypes.c_int64,
                           ctypes.c_int64, ctypes.c_int, ctypes.c_int32, ctypes.c_double, ctypes.c_int64, ctypes.c_int,
                           _P(np.int64), _P(np.float64), _P(np.float64), ctypes.c_int]
_ox, _osz, _oah = np.zeros(4096, np.int64), np.zeros(4096), np.zeros(4096)


class Arr:
    """Contiguous typed arrays of one dsim market for the C maker."""

    def __init__(self, m):
        B, P = m["book"], m["pr"]
        self.bx = np.ascontiguousarray(B["x"], np.int64)
        self.bb = np.ascontiguousarray(B["b"], np.int32)
        self.bk = np.ascontiguousarray(B["k"], np.int32)
        self.bbs = np.ascontiguousarray(B["bs"], np.float32)
        self.bks = np.ascontiguousarray(B["ks"], np.float32)
        self.px = np.ascontiguousarray(P["x"], np.int64)
        self.phb = np.ascontiguousarray(P["hb"], np.uint8)
        self.pup = np.ascontiguousarray(P["up"], np.int32)
        self.psz = np.ascontiguousarray(P["sz"], np.float64)
        self.ok = (self.bb > 0) & (self.bk > 0)

    def zeros(self):
        return np.zeros(len(self.px))

    def maker(self, t_arr, tok, price, size, t_cxl, model, used):
        n = _lib.lt1_maker(self.bx, self.bb, self.bk, self.bbs, self.bks, len(self.bx), self.px, self.phb, self.pup, self.psz,
                           used, len(self.px), int(t_arr), int(tok), int(price), float(size), int(t_cxl), int(model),
                           _ox, _osz, _oah, len(_ox))
        if n == -1:
            return "nobook", []
        if n == -2:
            return "rejected", []
        assert n <= len(_ox)
        return "ok", [(int(_ox[i]), float(_osz[i]), float(_oah[i])) for i in range(n)]

    def seen(self, t):
        return int(np.searchsorted(self.bx, t - BOOK, "right")) - 1

    def mids(self, ts, tok):
        """Token mid (1e-4) of the book in force at each t (inclusive); nan if not two-sided."""
        i = np.searchsorted(self.bx, np.asarray(ts, np.int64), "right") - 1
        out = np.full(len(i), np.nan)
        g = i >= 0
        gi = i[g]
        u = np.where(self.ok[gi], (self.bb[gi].astype(float) + self.bk[gi]) / 2, np.nan)
        out[g] = u if tok == 0 else 10000 - u
        return out


# ------------------------------------------------------------------ V0


def v0(day, n_orders=3000):
    """C maker vs dsim_strict.maker_strict on random real orders (both tokens, prices around the touch)."""
    import dsim_strict

    rng = np.random.default_rng(1)
    tot = bad = 0
    for tf in (5, 15):
        for h, mk in pmlat.hours(day, "btc", tf, hrs=range(8, 12), with_act=False):
            for e, m in mk.items():
                A = Arr(m)
                end = (e + tf * 60) * 1000
                for _ in range(max(1, n_orders // (2 * 4 * (12 if tf == 5 else 4)))):
                    t = int(rng.integers(e * 1000, end - 20000))
                    i = A.seen(t)
                    if i < 0 or not A.ok[i]:
                        continue
                    tok = int(rng.integers(0, 2))
                    bid = int(A.bb[i]) if tok == 0 else 10000 - int(A.bk[i])
                    price = bid - 100 * int(rng.integers(-1, 4))
                    if not 100 <= price <= 9900:
                        continue
                    t_cxl = t + int(rng.integers(1000, 120000))
                    for model, mi in (("pess", 0), ("prop", 1), ("front", 2)):
                        r = dsim_strict.maker_strict(m, t, tok, price, SIZE, t_cxl, model=model)
                        st, f = A.maker(t, tok, price, SIZE, t_cxl, mi, A.zeros())
                        py = [(int(z["x"]), round(z["size"], 9), round(z["ahead"], 6)) for z in r["fills"]]
                        cc = [(x, round(s, 9), round(a, 6)) for x, s, a in f]
                        tot += 1
                        if r["status"] != st or py != cc:
                            bad += 1
                            if bad <= 5:
                                print("MISMATCH", tf, e, tok, price, t, t_cxl, model, r["status"], st, py[:3], cc[:3])
    print(f"v0 {day}: {tot} order sims, {bad} mismatches")
    return bad == 0


# ------------------------------------------------------------------ Binance pull events


def bn_hours(day, h):
    """BTC Binance rows from hour files h-1..h+1 of `day` (pmlat's hour naming)."""
    from datetime import date, timedelta

    parts = []
    for hh in (h - 1, h, h + 1):
        d, x = date.fromisoformat(day), hh
        if x < 0:
            d, x = d - timedelta(days=1), x + 24
        elif x > 23:
            d, x = d + timedelta(days=1), x - 24
        if pmlat.MAXDAY and d.isoformat() > pmlat.MAXDAY:
            continue
        f = pmlat.REC / d.isoformat() / f"bn-{x:02d}.bin"
        if f.exists():
            a = np.fromfile(f, dtype=dsim.BN_DT)
            parts.append(a[a["a"] == 0])
    return np.concatenate(parts) if parts else np.zeros(0, dsim.BN_DT)


def pull_events(bn):
    """{pull: {tok: sorted receive ms of moves against tok}} from Binance USD-M BTC trades."""
    f = bn[bn["src"] == 1]
    f = f[np.argsort(f["T"], kind="stable")]
    T, p, rcv = f["T"].astype(np.int64), f["p"].astype(float), (f["l"] // 1000).astype(np.int64)
    out = {}
    for name, spec in PULLS.items():
        if spec is None:
            continue
        thr, look = spec
        j = np.searchsorted(T, T - look, "right") - 1
        ok = j >= 0
        r = np.full(len(T), 0.0)
        r[ok] = (p[ok] / p[j[ok]] - 1) * 1e4
        out[name] = {0: np.sort(rcv[r <= -thr]), 1: np.sort(rcv[r >= thr])}  # UP hurt by falls; DOWN by rises
    return out


# ------------------------------------------------------------------ fees


def fee_rate(tf, e):
    global FEES
    if FEES is None:
        import csv
        from datetime import datetime

        FEES = {}
        for r in csv.DictReader(open(HERE / "reports" / "dublin" / "fees_by_window.csv")):
            if r["asset"] == "btc" and r["fee_rate"] not in ("", None):
                t = int(datetime.fromisoformat(r["window_start_utc"].replace("Z", "+00:00")).timestamp())
                FEES[(int(r["tf"].rstrip("m")), t)] = float(r["fee_rate"])
    if (tf, e) in FEES:
        return FEES[(tf, e)], "census"
    return (0.07 if e < 1763683380 else 0.05), "rule"  # 2026-11-21 00:03 UTC


# ------------------------------------------------------------------ one window


def tp_sim(A, tok, fx, fsz, fpx, x, end, model, used):
    """Maker TP sell of `fsz` shares of tok bought at fpx. -> (tp price, [(t, size)] fills up to the close, arrival)."""
    t_dec = fx + BOOK
    for _ in range(3):
        if t_dec >= end:
            return None, [], None
        i = A.seen(t_dec)
        if i < 0 or not A.ok[i]:
            return None, [], None
        bid = int(A.bb[i]) if tok == 0 else 10000 - int(A.bk[i])
        ask = int(A.bk[i]) if tok == 0 else 10000 - int(A.bb[i])
        tp = min(fpx + 100 * x, 9900)
        if bid >= tp:
            tp = ask
        t_arr = t_dec + pmlat.olat(A.m, t_dec, SC)
        if t_arr >= end:
            return None, [], None
        st, f = A.maker(t_arr, 1 - tok, 10000 - tp, fsz, end, model, used)
        if st == "ok":
            return tp, [(a, b) for a, b, _ in f], t_arr
        if st == "nobook":
            return None, [], None
        t_dec = t_arr + BOOK
    return None, [], None


def run_window(m, tf, e, winner, pev, rows, rungs, models=MODELS):
    A = Arr(m)
    A.m = m
    t0, end = e * 1000, (e + tf * 60) * 1000
    t_dec = t0 + 1000
    i = A.seen(t_dec)
    if i < 0 or not A.ok[i]:
        rungs.append((tf, e, -1, "", "", -1, -1, -1, "nobook", -1))
        return
    ol = pmlat.olat(m, t_dec, SC)
    t_arr = t_dec + ol
    base_cxl = max(end - 60000 + CXL, t_arr + CXL)
    fr, fsrc = fee_rate(tf, e)
    ex_dec = end - 30000
    ex_match = ex_dec + pmlat.olat(m, ex_dec, SC) + HOLD
    for tok in (0, 1):
        anchor = int(A.bb[i]) if tok == 0 else 10000 - int(A.bk[i])
        if isinstance(winner, float):  # synthetic validation: expected payout of UP
            pay = winner if tok == 0 else 1.0 - winner
        else:
            pay = np.nan if winner is None else float((winner == "UP") == (tok == 0))
        tk = dsim.taker(m, ex_match, tok, 100, size=1e9, min_show=1e-6, sell=True)
        tk_px, tk_sz = (tk["px"], tk["size"]) if tk else (None, 0.0)
        tk_mid = A.mids([ex_match], tok)[0]
        for pull in PULLS:
            t_cxl = base_cxl
            if PULLS[pull] is not None:
                ev = pev[pull][tok]
                k = int(np.searchsorted(ev, t_dec, "right"))
                if k < len(ev):
                    t_cxl = max(min(t_cxl, int(ev[k]) + CXL), t_arr + CXL)
            for mname, mi in models.items():
                for step in STEPS:
                    for off in OFFS:
                        used = A.zeros()
                        used_tp = {x: A.zeros() for x in XS}
                        for r in range(KMAX):
                            price = anchor - 100 * off - 100 * step * r
                            if price < FLOOR:
                                rungs.append((tf, e, tok, pull, mname, step, off, r, "skip", price))
                                continue
                            st, fills = A.maker(t_arr, tok, price, SIZE, t_cxl, mi, used)
                            rungs.append((tf, e, tok, pull, mname, step, off, r, st if fills or st != "ok" else "nofill", price))
                            if not fills:
                                continue
                            fxs = np.array([f[0] for f in fills], np.int64)
                            mo = {h: (A.mids(fxs + h, tok) - price) / 100 for h in (1000, 5000, 30000)}
                            for n, (fx, fsz, ah) in enumerate(fills):
                                row = [tf, e, tok, pull, mname, step, off, r, price, anchor, ol, t_cxl - t_arr, fx, round(fsz, 6),
                                       ah, round(mo[1000][n], 3), round(mo[5000][n], 3), round(mo[30000][n], 3), pay, fr]
                                for x in XS:
                                    tp, tpf, tpa = tp_sim(A, tok, fx, fsz, price, x, end, mi, used_tp[x])
                                    s1 = sum(z[1] for z in tpf)
                                    s2 = sum(z[1] for z in tpf if z[0] <= ex_dec + CXL)
                                    tpp = (tp or 0) / 1e4
                                    p = price / 1e4
                                    pnl1 = s1 * tpp + (fsz - s1) * pay - fsz * p
                                    rem = fsz - s2
                                    q = min(rem, tk_sz) if tk_px is not None else 0.0
                                    if q > 1e-9:
                                        xp = tk_px / 1e4
                                        exit_rev = q * (xp - fr * xp * (1 - xp))
                                    else:
                                        exit_rev = 0.0
                                    pnl2 = s2 * tpp + exit_rev + (rem - q) * pay - fsz * p
                                    tfirst = tpf[0][0] if tpf else -1
                                    row += [tp or 0, round(s1, 6), round(s2, 6), round(pnl1, 6), round(pnl2, 6), tfirst]
                                row += [tk_px or 0, round(tk_mid, 1) if tk_mid == tk_mid else np.nan]
                                rows.append(row)


COLS = (["tf", "e", "tok", "pull", "q", "step", "off", "rung", "px", "anchor", "olat", "t_live", "fx", "fsz", "ahead",
         "mo1", "mo5", "mo30", "pay", "fee_rate"]
        + [f"{c}{x}" for x in XS for c in ("tp", "tps1_", "tps2_", "pnl1_", "pnl2_", "tpt")]
        + ["exit_px", "exit_mid"])
RCOLS = ["tf", "e", "tok", "pull", "q", "step", "off", "rung", "status", "px"]


def write(path, cols, rows):
    with gzip.open(path, "wt") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join("" if (isinstance(v, float) and v != v) else str(v) for v in r) + "\n")


def slug(tf, e):
    return f"btc-updown-{tf}m-{e}"


def sim_day(day, tfs=(5, 15)):
    import resource

    OUT.mkdir(parents=True, exist_ok=True)
    W = pmlat.winners([])
    for tf in tfs:
        rows, rungs = [], []
        nw = 0
        for h, mk in pmlat.hours(day, "btc", tf, with_act=False):
            miss = [slug(tf, e) for e in mk if slug(tf, e) not in W]
            if miss:
                W.update(pmlat.winners(miss))
            pev = pull_events(bn_hours(day, h))
            for e, m in sorted(mk.items()):
                run_window(m, tf, e, W.get(slug(tf, e)), pev, rows, rungs)
                nw += 1
            print(day, tf, h, nw, len(rows), "maxrss_MB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024, flush=True)
        base = OUT / f"lt1-{day}-{tf}m"
        write(f"{base}.csv.gz", COLS, rows)
        write(f"{base}-rungs.csv.gz", RCOLS, rungs)
        print("wrote", base, nw, "windows", len(rows), "fill events", flush=True)


if __name__ == "__main__":
    cmd, day = sys.argv[1], sys.argv[2]
    if cmd == "v0":
        sys.exit(0 if v0(day) else 1)
    elif cmd == "day":
        sim_day(day, tuple(int(t) for t in sys.argv[3:]) or (5, 15))
