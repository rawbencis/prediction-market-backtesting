"""D2 cell tables from d2_samples.py output.

usage: d2_cells.py grid SAMPLES.npz FEE [--tf 5]        level map (84 cells; 15m adds two tau buckets), cell W
       d2_cells.py final SAMPLES.npz FEE                 final-minute table (ask x seconds left; fair value x seconds left)
       d2_cells.py cells SAMPLES.npz FEE CELLS.json      evaluate frozen cells only
One taker BUY per window per side per cell (the first sample of that side in the cell). SE by window (cluster-robust).
edge_mid = win - mid at the decision; edge_net = win - paid - FEE p(1-p) over filled orders; pess = the same over
orders that also pass the pessimistic fill; fill = filled / tried.
"""
import json
import math
import sys

import numpy as np

Z_E = (-np.inf, -2, -1, -0.3, 0.3, 1, 2, np.inf)
Z_L = ("<=-2", "(-2,-1]", "(-1,-0.3]", "(-0.3,0.3]", "(0.3,1]", "(1,2]", ">2")
R_E, R_L = (0, 0.7, 1.4001, np.inf), ("<0.7", "0.7-1.4", ">1.4")


def tau_edges(tf):
    return ((10, 60, 120, 180, 290.001), ("10-60", "60-120", "120-180", "180-290")) if tf == 5 else \
           ((10, 60, 120, 180, 290, 600, 890.001), ("10-60", "60-120", "120-180", "180-290", "290-600", "600-890"))


def load(path):
    f = np.load(path)
    c = list(f["cols"])
    return {k: f["data"][:, i] for i, k in enumerate(c)}


def zbin(z):  # right-closed buckets: (lo, hi]
    return np.searchsorted(np.array(Z_E[1:-1]), z, "left")


def cl_mean_se(x, w):
    """Mean of x and its window-cluster SE."""
    x, w = np.asarray(x, float), np.asarray(w)
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else float("nan")), float("nan")
    m = x.mean()
    _, inv = np.unique(w, return_inverse=True)
    s = np.bincount(inv, x - m)
    return float(m), float(math.sqrt((s * s).sum()) / n)


def first_per(keys, t):
    """Index of the first sample (min t) for each distinct key tuple."""
    o = np.lexsort((t,) + tuple(keys[::-1]))
    ks = np.stack([k[o] for k in keys], 1)
    new = np.r_[True, (np.diff(ks, axis=0) != 0).any(1)]
    return o[new]


def stats(D, idx, fee):
    if len(idx) == 0:
        return None
    w, win = D["e"][idx], D["win"][idx]
    f = D["filled"][idx] > 0
    pf = D["pess"][idx] > 0
    paid = D["paid"][idx]
    net = win - paid - fee * paid * (1 - paid)
    mid_ok = np.isfinite(D["mid"][idx])
    def floor(m_se, price, n):  # binomial floor at the paid price: a sample with no losses has no SE otherwise
        m, se = m_se
        if n == 0 or m != m:
            return m_se
        q = float(np.mean(price))
        return m, max(se, math.sqrt(max(q * (1 - q), 1e-6) / n)) if se == se else math.sqrt(max(q * (1 - q), 1e-6) / n)
    em = floor(cl_mean_se((win - D["mid"][idx])[mid_ok], w[mid_ok]), D["mid"][idx][mid_ok], int(mid_ok.sum())) if mid_ok.any() else (np.nan, np.nan)
    en = floor(cl_mean_se(net[f], w[f]), paid[f], int(f.sum())) if f.sum() else (np.nan, np.nan)
    ep = floor(cl_mean_se(net[pf], w[pf]), paid[pf], int(pf.sum())) if pf.sum() else (np.nan, np.nan)
    return dict(n=len(idx), nw=len(np.unique(w)), ask=D["ask"][idx].mean(), mid=np.nanmean(D["mid"][idx]),
                phi=D["phi"][idx].mean(), win=win.mean(), em=em, en=en, ep=ep, fill=f.mean(), pfill=pf.mean(),
                fee=float(np.mean(fee * paid[f] * (1 - paid[f]))) if f.sum() else np.nan)


def fmt(s, tag=""):
    if s is None:
        return "| " + tag + " | 0 |" + " |" * 9
    c = lambda m: f"{100 * m[0]:+.2f} ± {100 * m[1]:.2f}" if m[0] == m[0] else ""
    return (f"| {tag} | {s['nw']} | {100 * s['ask']:.1f} | {100 * s['mid']:.1f} | {100 * s['phi']:.1f} | {s['win']:.3f} | "
            f"{c(s['em'])} | {c(s['en'])} | {c(s['ep'])} | {s['fill']:.2f} / {s['pfill']:.2f} |")


HDR = ("| cell | windows | mean ask ¢ | mean mid ¢ | mean Φ(z) ¢ | win rate | edge_mid ¢ ± SE | edge_net ¢ ± SE | "
       "edge_net pess ¢ ± SE | fill / pess fill |\n|---|---|---|---|---|---|---|---|---|---|")


def grid(D, fee, tf, ksub=None):
    T_E, T_L = tau_edges(tf)
    m = np.isfinite(D["r"]) & (D["tau"] >= 10) & (D["tau"] <= T_E[-1])
    if ksub is not None:
        m &= D["ksrc"] == ksub
    zb, tb = zbin(D["z"]), np.searchsorted(np.array(T_E), D["tau"], "right") - 1
    rb = np.searchsorted(np.array(R_E), D["r"], "right") - 1
    cell = zb * 100 + tb * 10 + rb
    sel = np.flatnonzero(m)
    fi = sel[first_per([D["e"][sel], D["side"][sel], cell[sel]], D["t"][sel])]
    res = {}
    for c in np.unique(cell[fi]):
        res[int(c)] = stats(D, fi[cell[fi] == c], fee)
    return res, T_L


def neighbours(c, nt):
    z, t, r = c // 100, (c // 10) % 10, c % 10
    out = []
    for dz, dt, dr in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        if 0 <= z + dz < 7 and 0 <= t + dt < nt and 0 <= r + dr < 3:
            out.append((z + dz) * 100 + (t + dt) * 10 + r + dr)
    return out


def label(c, T_L):
    return f"z {Z_L[c // 100]}, τ {T_L[(c // 10) % 10]}, r {R_L[c % 10]}"


def cell_w(D, fee, tau_lo=120, tau_hi=290):
    m = (D["d"] >= 5) & (D["d"] <= 20) & (D["tau"] >= tau_lo) & (D["tau"] <= tau_hi) & (np.abs(D["mv1"]) < 0.5) & \
        np.isfinite(D["mid"]) & (D["ask"] <= D["mid"] + 0.01 + 1e-9) & np.isfinite(D["r"])
    out = []
    rb = np.searchsorted(np.array(R_E), D["r"], "right") - 1
    for name, mm in [("all r", m)] + [(f"r {R_L[i]}", m & (rb == i)) for i in range(3)]:
        sel = np.flatnonzero(mm)
        fi = sel[first_per([D["e"][sel], D["side"][sel]], D["t"][sel])] if len(sel) else sel
        out.append((name, stats(D, fi, fee)))
    return out


def main():
    mode, path, fee = sys.argv[1], sys.argv[2], float(sys.argv[3])
    D = load(path)
    tf = int(sys.argv[sys.argv.index("--tf") + 1]) if "--tf" in sys.argv else 5
    if mode == "grid":
        res, T_L = grid(D, fee, tf)
        nt = len(T_L)
        print(f"Samples: {len(D['e']):,} side-samples, {len(np.unique(D['e']))} windows; Chainlink strike for "
              f"{np.mean([D['ksrc'][D['e'] == e][0] for e in np.unique(D['e'])]):.0%} of windows.\n")
        print(HDR)
        cand = []
        for c, s in sorted(res.items()):
            print(fmt(s, label(c, T_L)))
            en, se = s["en"]
            if s and en == en and se > 0 and en / se >= 2.5 and any(
                    res.get(nb) and res[nb]["en"][0] > 0 for nb in neighbours(c, nt)):
                cand.append((en / se, c))
        ts = [s["en"][0] / s["en"][1] for s in res.values() if s and s["en"][1] > 0]
        print(f"\nCells with trades: {len(res)}; |t| >= 2 on edge_net: {sum(abs(x) >= 2 for x in ts)} "
              f"(positive {sum(x >= 2 for x in ts)}, negative {sum(x <= -2 for x in ts)}); "
              f"chance alone ≈ {0.0455 * len(ts):.1f} at |t| >= 2 (half each sign) if cells were independent.")
        ses = sorted(s["en"][1] for s in res.values() if s and s["en"][1] == s["en"][1])
        print(f"Smallest detectable edge (2.5 SE) per cell: median {250 * ses[len(ses) // 2]:.1f}¢, best {250 * ses[0]:.1f}¢.")
        print("\nFreeze candidates (edge_net > 0 at t >= 2.5 with a positive same-sign neighbour): " +
              ("; ".join(f"{label(c, T_L)} t={t:.2f}" for t, c in sorted(cand, reverse=True)) or "none"))
        for ks, nm in ((1, "Chainlink strike"), (0, "Binance-proxy strike")):
            r2, _ = grid(D, fee, tf, ks)
            pooled = {}
            for c, s in r2.items():
                pooled.setdefault(c // 100, []).append(s)
            print(f"\nBy z bucket, {nm} windows only (cells pooled over τ and r, trade-weighted):")
            print("| z | trades | edge_mid ¢ | edge_net ¢ |\n|---|---|---|---|")
            for z, ss in sorted(pooled.items()):
                n = sum(s["n"] for s in ss)
                emv = sum(s["em"][0] * s["n"] for s in ss if s["em"][0] == s["em"][0]) / n
                env = sum(s["en"][0] * s["n"] * s["fill"] for s in ss if s["en"][0] == s["en"][0]) / max(1e-9, sum(s["n"] * s["fill"] for s in ss if s["en"][0] == s["en"][0]))
                print(f"| {Z_L[z]} | {n:,} | {100 * emv:+.2f} | {100 * env:+.2f} |")
        print("\nCell W (d +5..+20 bp, τ 120-290, |S move 1 s| < 0.5 bp, ask <= mid + 1¢):\n")
        print(HDR)
        for nm, s in cell_w(D, fee):
            print(fmt(s, nm))
        if tf == 15:
            print("\nCell W on 15m with τ 120-890:\n")
            print(HDR)
            for nm, s in cell_w(D, fee, 120, 890):
                print(fmt(s, nm))
    elif mode == "final":
        ASK = ((0.90, 0.95, "0.90-0.95"), (0.95, 0.97, "0.95-0.97"), (0.97, 0.99, "0.97-0.99"))
        SL = ((40, 60, "60-40 s"), (20, 40, "40-20 s"), (5, 20, "20-5 s"))
        FV = ((0, 0.90, "FV <0.90"), (0.90, 0.95, "FV 0.90-0.95"), (0.95, 0.97, "FV 0.95-0.97"), (0.97, 0.99, "FV 0.97-0.99"), (0.99, 1.01, "FV 0.99+"))
        a_in = (D["ask"] >= 0.90) & (D["ask"] < 0.99 - 1e-9)
        print("| ask × seconds left | windows | win rate | ask + fee ¢ | edge_net ¢ ± SE | edge_net pess ¢ ± SE | fill / pess fill |\n|---|---|---|---|---|---|---|")
        for lo, hi, an in ASK:
            for sl, sh, sn in SL:
                m = (D["ask"] >= lo - 1e-9) & (D["ask"] < hi - 1e-9) & (D["tau"] > sl) & (D["tau"] <= sh)
                sel = np.flatnonzero(m)
                fi = sel[first_per([D["e"][sel], D["side"][sel]], D["t"][sel])] if len(sel) else sel
                s = stats(D, fi, fee)
                if s is None:
                    print(f"| {an}, {sn} | 0 | | | | | |"); continue
                c = lambda m_: f"{100 * m_[0]:+.2f} ± {100 * m_[1]:.2f}" if m_[0] == m_[0] else ""
                print(f"| {an}, {sn} | {s['nw']} | {s['win']:.3f} | {100 * (s['ask'] + fee * s['ask'] * (1 - s['ask'])):.2f} | {c(s['en'])} | {c(s['ep'])} | {s['fill']:.2f} / {s['pfill']:.2f} |")
        ok = np.isfinite(D["fvcl"])
        print(f"\nBy TWAP-aware fair value from the Chainlink stream (Chainlink S and twap60 K; {ok.mean():.0%} of samples), ask 0.90-0.99:\n")
        print("| FV × seconds left | windows | mean ask ¢ | mean FV ¢ | win rate | edge_net ¢ ± SE | fill |\n|---|---|---|---|---|---|---|")
        for lo, hi, fn in FV:
            for sl, sh, sn in SL:
                m = ok & a_in & (D["fvcl"] >= lo) & (D["fvcl"] < hi) & (D["tau"] > sl) & (D["tau"] <= sh)
                sel = np.flatnonzero(m)
                fi = sel[first_per([D["e"][sel], D["side"][sel]], D["t"][sel])] if len(sel) else sel
                s = stats(D, fi, fee)
                if s is None:
                    continue
                c = lambda m_: f"{100 * m_[0]:+.2f} ± {100 * m_[1]:.2f}" if m_[0] == m_[0] else ""
                print(f"| {fn}, {sn} | {s['nw']} | {100 * s['ask']:.1f} | {100 * np.mean(D['fvcl'][fi]):.1f} | {s['win']:.3f} | {c(s['en'])} | {s['fill']:.2f} |")
    elif mode == "cells":
        spec = json.load(open(sys.argv[4]))
        res, T_L = grid(D, fee, tf)
        print(HDR)
        for c in spec["cells"]:
            print(fmt(res.get(c["code"]), label(c["code"], T_L)))


if __name__ == "__main__":
    main()
