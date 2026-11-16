"""Empirical fair-value map for up/down books: where does the book misprice a side, and can a taker collect it?

For every settled window and every STEP on the recorder's clock (decision time = local receipt clock):
  d_s    = Binance move toward the side since the open, in bp: ln(S_seen / S_open), S_open = mean Binance price over
           the 60 s before the window start (proxy for the Chainlink 60 s TWAP strike), S_seen = last Binance trade
           this box had received at the decision time
  t_left = seconds to the window end
  the taker order: limit = the side's ask as seen at the decision + CAP; it matches LAT ms later against the book
           standing then on the exchange clock (recorded book), and fills only if that ask <= limit with >= SIZE shown
  EV     = win - ask paid - taker fee, per share
Cells are d_s bucket x t_left bucket, both sides pooled. Samples within one window are not independent, so every
cell's mean is a mean of per-window means and its 90% CI bootstraps windows.

  python3 fair_map.py --rec ~/shadow-rec [--days D ...] [--asset btc] [--tf 5] [--src spot|fut] [--lat-ms 72]

Exploratory: any rule read off this map gets frozen and tested on days after the ones used here.
"""

import argparse
import calendar
import time
from pathlib import Path

import numpy as np

import shadow_eval as se
from leadlag import fee

ASSETS = ("btc", "eth", "sol", "xrp", "doge")
D_EDGES = (-1e9, -10, -5, -2, 0, 2, 5, 10, 20, 1e9)
T_EDGES = (0, 10, 30, 60, 90, 120, 180, 240, 301)


def _hour_files(root, days, kind):
    out = []
    for d in days:
        for f in sorted((root / d).glob(f"{kind}-*.bin")):
            out.append(((d, int(f.stem.split("-")[1])), f))
    return out


def _read(f, dt, keep):
    x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
    return x[keep(x)]


def label(edges, v):
    i = int(np.searchsorted(edges, v, "right")) - 1
    lo, hi = edges[i], edges[i + 1]
    return f"{'' if lo < -1e8 else f'{lo:g}'}..{'' if hi > 1e8 else f'{hi:g}'}"


def window_samples(e, tf, side_win, bn, pm, a):
    """All samples of one window: list of (d_s, t_left, filled, ev, ask_paid, win)."""
    e_ms, end = e * 1000, (e + 60 * tf) * 1000
    pre = bn[(bn["T"] >= e_ms - 60_000) & (bn["T"] < e_ms)]
    if len(pre):
        s_open = float(pre["p"].mean())
    else:
        after = bn[bn["T"] >= e_ms]
        if not len(after):
            return []
        s_open = float(after["p"][0])
    bl = bn["l"] // 1000  # receipt ms
    o = np.argsort(bl, kind="stable")
    bl, bp = bl[o], bn["p"][o]
    pl = pm["l"] // 1000
    o1 = np.argsort(pl, kind="stable")
    seen = pm[o1]
    pl = pl[o1]
    o2 = np.argsort(pm["x"], kind="stable")
    ex = pm[o2]
    out = []
    for tau in range(e_ms + a.step_ms, end - 500, a.step_ms):
        i = np.searchsorted(bl, tau, "right") - 1
        j = np.searchsorted(pl, tau, "right") - 1
        k = np.searchsorted(ex["x"], tau + a.lat_ms, "right") - 1
        if i < 0 or j < 0 or k < 0 or seen["k"][j] <= 0 or ex["k"][k] <= 0:
            continue
        d = 1e4 * np.log(float(bp[i]) / s_open)
        t_left = (end - tau) / 1000
        for side in (0, 1):
            ask_seen = seen["k"][j] / 1e4 if side == 0 else 1 - seen["b"][j] / 1e4
            ask = ex["k"][k] / 1e4 if side == 0 else 1 - ex["b"][k] / 1e4
            size = float(ex["ks"][k] if side == 0 else ex["bs"][k])
            if not (a.min_ask <= ask_seen <= a.max_ask):
                continue
            ok = ask <= ask_seen + a.cap + 1e-9 and size >= a.size
            win = 1.0 if side_win == side else 0.0
            out.append((d if side == 0 else -d, t_left, ok, (win - ask - fee(ask, a.fee_rate)) if ok else np.nan, ask, win))
    return out


def build(a):
    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if any(p.glob("pm-*.bin")))
    ai = ASSETS.index(a.asset)
    pm_files = dict(_hour_files(a.rec, days, "pm"))
    bn_files = dict(_hour_files(a.rec, days, "bn"))
    hours = sorted(set(pm_files) & set(bn_files))
    keys = []
    for d, h in hours:
        t0 = calendar.timegm(time.strptime(f"{d} {h:02d}", "%Y-%m-%d %H"))
        keys += [e for e in range(t0, t0 + 3600, 60 * a.tf)]
    slugs = [f"{a.asset}-updown-{a.tf}m-{e}" for e in keys]
    win = se.settle(a.settle, slugs, not a.no_fetch)
    cache = {}

    def hour(hk):
        if hk not in cache:
            pmf, bnf = pm_files.get(hk), bn_files.get(hk)
            cache[hk] = (
                _read(pmf, se.PM_DT, lambda x: (x["a"] == ai) & (x["tf"] == a.tf)) if pmf else np.zeros(0, se.PM_DT),
                _read(bnf, se.BN_DT, lambda x: (x["a"] == ai) & (x["src"] == a.src)) if bnf else np.zeros(0, se.BN_DT),
            )
        return cache[hk]

    rows, wid = [], 0
    for idx, hk in enumerate(hours):
        near = [hours[j] for j in (idx - 1, idx, idx + 1) if 0 <= j < len(hours)]
        for old in [k for k in cache if k not in near]:
            cache.pop(old)  # keep memory flat: at most three filtered hours in memory
        parts = [hour(x) for x in near]
        pm = np.concatenate([p for p, _ in parts])
        bn = np.concatenate([b for _, b in parts])
        d, h = hk
        t0 = calendar.timegm(time.strptime(f"{d} {h:02d}", "%Y-%m-%d %H"))
        for e in range(t0, t0 + 3600, 60 * a.tf):
            w = win.get(f"{a.asset}-updown-{a.tf}m-{e}")
            if w not in ("UP", "DOWN"):
                continue
            P = pm[pm["e"] == e]
            if not len(P):
                continue
            B = bn[(bn["T"] >= e * 1000 - 61_000) & (bn["T"] <= (e + 60 * a.tf) * 1000)]
            for s in window_samples(e, a.tf, 0 if w == "UP" else 1, B, P, a):
                rows.append((wid, *s))
            wid += 1
    dt = np.dtype([("w", "<i4"), ("d", "<f4"), ("t", "<f4"), ("ok", "?"), ("ev", "<f4"), ("ask", "<f4"), ("win", "<f4")])
    return np.array(rows, dtype=dt), wid, days


def cell_stats(R, n_boot=400, seed=7):
    """Mean of per-window mean EV (filled samples only), its 90% CI over windows, fill share, mean ask, win rate."""
    F = R[R["ok"]]
    if not len(F):
        return None
    ws, inv = np.unique(F["w"], return_inverse=True)
    sums = np.bincount(inv, weights=F["ev"].astype(float))
    cnt = np.bincount(inv)
    per = sums / cnt
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(per), (n_boot, len(per)))
    boot = per[idx].mean(1)
    lo, hi = np.percentile(boot, [5, 95])
    return dict(ev=100 * per.mean(), lo=100 * lo, hi=100 * hi, windows=len(ws), samples=len(F),
                fill=len(F) / len(R), ask=float(F["ask"].mean()), win=float(F["win"].mean()))  # fmt: skip


def report(R, n_windows, days, a, out=print):
    out(f"{a.asset} {a.tf}m, {days[0]}..{days[-1]}, {n_windows} settled windows, {len(R)} side-samples "
        f"(step {a.step_ms} ms, landing +{a.lat_ms} ms, cap {100 * a.cap:g}c, size {a.size:g}, signal {('spot', 'fut')[a.src]})")  # fmt: skip
    dl = [label(D_EDGES, D_EDGES[i]) if D_EDGES[i] > -1e8 else label(D_EDGES, D_EDGES[1] - 1) for i in range(len(D_EDGES) - 1)]
    tl = [f"{T_EDGES[i + 1]:g}-{T_EDGES[i]:g}s" for i in range(len(T_EDGES) - 2, -1, -1)]
    di = np.searchsorted(D_EDGES, R["d"], "right") - 1
    ti = np.searchsorted(T_EDGES, R["t"], "right") - 1
    out("\nEV c/share per cell (mean of per-window means; * = 90% CI above 0, . = below 0; blank = < 20 windows)")
    out(f"{'move toward side (bp)':>22} | " + " ".join(f"{c:>9}" for c in tl))
    cells = []
    for i, name in enumerate(dl):
        line = []
        for j in range(len(T_EDGES) - 2, -1, -1):
            s = cell_stats(R[(di == i) & (ti == j)])
            if not s or s["windows"] < 20:
                line.append(f"{'':>9}")
                continue
            mark = "*" if s["lo"] > 0 else "." if s["hi"] < 0 else " "
            line.append(f"{s['ev']:+8.2f}{mark}")
            cells.append((name, tl[len(T_EDGES) - 2 - j], s))
        out(f"{name:>22} | " + " ".join(line))
    out("\nBest cells (>= 20 windows), with fill share, mean ask paid and win rate")
    for name, t, s in sorted(cells, key=lambda c: -c[2]["lo"])[: a.top]:
        out(f"  move {name:>8} bp, {t:>8} left: EV {s['ev']:+6.2f}c [{s['lo']:+6.2f}, {s['hi']:+6.2f}]  "
            f"windows {s['windows']:4d}  fill {s['fill']:5.0%}  ask {s['ask']:.3f}  win {s['win']:.3f}")  # fmt: skip
    return cells


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--asset", default="btc", choices=ASSETS)
    ap.add_argument("--tf", type=int, default=5, choices=(5, 15))
    ap.add_argument("--src", type=lambda s: {"spot": 0, "fut": 1}[s], default=0, help="spot | fut")
    ap.add_argument("--step-ms", type=int, default=1000)
    ap.add_argument("--lat-ms", type=int, default=72, help="decision -> match: order leg ~22 + 50 ms taker hold")
    ap.add_argument("--cap", type=float, default=0.01)
    ap.add_argument("--size", type=float, default=5.0)
    ap.add_argument("--min-ask", type=float, default=0.02)
    ap.add_argument("--max-ask", type=float, default=0.99)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--settle", type=Path, default=Path.home() / "act-settlements.csv")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    R, n, days = build(a)
    if not len(R):
        print("no samples")
        return
    report(R, n, days, a)


if __name__ == "__main__":
    main()
