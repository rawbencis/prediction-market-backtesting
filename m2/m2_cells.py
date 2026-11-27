"""M2-1 cell tables (frozen/M2_plan.json) from m2_samples.py output (~/m2-out/s/<asset><tf>-DAY-<match_ms>.npz).

Eligible sample: 5 < tau <= 60, finite fvcl, side ask <= 0.30 with >= 5 shown. g = fvcl - ask - fee ask (1 - ask).
Cells: g {5-10, 10-20, >=20}c x seconds left {60-40, 40-20, 20-5} x ask {<=5, 5-15, 15-30}c, and the same split by
strike-gap sign (toward = kgap > 0). One FAK per window per side per cell at its first sample. Statistics from
dublin1124/d2_cells.stats (edge_net over filled orders, window-cluster SE floored at the binomial SE).

usage: m2_cells.py [--json OUT.json]   (prints markdown; writes the freeze decision to OUT.json)
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dublin1124"))
import d2_cells as C  # noqa: E402

S = Path.home() / "m2-out/s"
MARKETS = (("btc", 5, 0.05), ("btc", 15, 0.05), ("eth", 5, 0.01))
DAYS = [f"2026-11-{d}" for d in range(14, 24)]
H1 = set(DAYS[:5])
G = ((0.05, 0.10, "g 5–10¢"), (0.10, 0.20, "g 10–20¢"), (0.20, 9.0, "g ≥ 20¢"))
TAU = ((40, 60, "60–40 s"), (20, 40, "40–20 s"), (5, 20, "20–5 s"))
ASK = ((-1, 0.05, "ask ≤ 5¢"), (0.05, 0.15, "5–15¢"), (0.15, 0.30, "15–30¢"))


def load(asset, tf, ms):
    parts, cols = [], None
    for d in DAYS:
        f = S / f"{asset}{tf}-{d}-{ms}.npz"
        if not f.exists():
            print(f"MISSING {f}", file=sys.stderr)
            continue
        z = np.load(f)
        cols = list(z["cols"])
        if len(z["data"]):
            parts.append(z["data"])
    X = np.concatenate(parts)
    D = {k: X[:, i] for i, k in enumerate(cols)}
    D["day"] = np.array([str(np.datetime64(int(e), "s").astype("datetime64[D]")) for e in D["e"]])
    return D


def cells(D, fee):
    ask, tau, fv = D["ask"], D["tau"], D["fvcl"]
    g = fv - ask - fee * ask * (1 - ask)
    el = np.isfinite(fv) & (ask <= 0.30 + 1e-9) & (D["asz"] >= 5) & (tau > 5) & (tau <= 60)
    gb = np.full(len(ask), -1)
    tb = np.full(len(ask), -1)
    ab = np.full(len(ask), -1)
    for i, (lo, hi, _) in enumerate(G):
        gb[(g >= lo - 1e-12) & (g < hi)] = i
    for i, (lo, hi, _) in enumerate(TAU):
        tb[(tau > lo) & (tau <= hi + 1e-9)] = i
    for i, (lo, hi, _) in enumerate(ASK):
        ab[(ask > lo + 1e-9) & (ask <= hi + 1e-9)] = i
    ok = el & (gb >= 0) & (tb >= 0) & (ab >= 0)
    return ok, gb * 100 + tb * 10 + ab, g


def evaluate(D, fee, extra=None, split=None):
    """returns {cell: stats dict} for the grid (optionally restricted by split mask)."""
    ok, cell, g = cells(D, fee)
    if split is not None:
        ok = ok & split
    sel = np.flatnonzero(ok)
    fi = sel[C.first_per([D["e"][sel], D["side"][sel], cell[sel]], D["t"][sel])] if len(sel) else sel
    res = {}
    for c in np.unique(cell[fi]):
        idx = fi[cell[fi] == c]
        s = C.stats(D, idx, fee)
        f = D["filled"][idx] > 0
        s["fw"] = len(np.unique(D["e"][idx][f]))
        s["fv"] = float(np.mean(D["fvcl"][idx]))
        paid = D["paid"][idx][f]
        s["roi"] = float(D["win"][idx][f].sum() / (paid + fee * paid * (1 - paid)).sum() - 1) if f.any() else np.nan
        for nm, hm in (("h1", np.isin(D["day"][idx], list(H1))), ("h2", ~np.isin(D["day"][idx], list(H1)))):
            j = idx[hm]
            s[nm] = C.stats(D, j, fee)["en"] if len(j) and (D["filled"][j] > 0).any() else (np.nan, np.nan)
        res[int(c)] = s
    return res


def lab(c):
    return f"{G[c // 100][2]}, {TAU[(c // 10) % 10][2]}, {ASK[c % 10][2]}"


def pm(m):
    return f"{100 * m[0]:+.2f} ± {100 * m[1]:.2f}" if m[0] == m[0] else "—"


HDR = ("| cell | windows | filled windows | fill / pess | mean ask ¢ | mean FV ¢ | win | edge_net ¢ ± SE | t | ROI/$ | "
       "pess edge ¢ | 14–18 | 19–23 | 2.5 SE ¢ | edge @330 ¢ | pess @430 ¢ |\n" + "|---" * 16 + "|")


def line(c, s, s3):
    en, se = s["en"]
    t = en / se if se and se == se and se > 0 else float("nan")
    e3 = pm(s3["en"]) if s3 else "—"
    p3 = pm(s3["ep"]) if s3 else "—"
    return (f"| {lab(c)} | {s['nw']} | {s['fw']} | {s['fill']:.2f} / {s['pfill']:.2f} | {100 * s['ask']:.1f} | {100 * s['fv']:.1f} | "
            f"{s['win']:.3f} | {pm(s['en'])} | {t:+.2f} | {100 * s['roi']:+.0f}% | {pm(s['ep'])} | {pm(s['h1'])} | {pm(s['h2'])} | "
            f"{250 * se:.1f} | {e3} | {p3} |"), t


def qualifies(s, t):
    return (s["fw"] >= 30 and s["en"][0] > 0 and t >= 2.5 and s["h1"][0] > 0 and s["h2"][0] > 0
            and s["ep"][0] == s["ep"][0] and s["ep"][0] > 0)


def main():
    outj = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    cands, summary = [], []
    for asset, tf, fee in MARKETS:
        D, D3 = load(asset, tf, 205), load(asset, tf, 330)
        print(f"\n## {asset.upper()} {tf}m (fee {fee})\n")
        okf = np.isfinite(D["fvcl"])
        ok, _, g = cells(D, fee)
        print(f"Samples: {len(D['e']):,} side-samples, {len(np.unique(D['e']))} windows; finite FV {okf.mean():.0%}; "
              f"in a cell {ok.sum():,} samples.\n")
        # context: pooled tail calibration by g (all samples eligible except the g bucket), first sample per window/side/bucket
        el = np.isfinite(D["fvcl"]) & (D["ask"] <= 0.30 + 1e-9) & (D["asz"] >= 5) & (D["tau"] > 5) & (D["tau"] <= 60)
        print("Context, all eligible tail samples (ask ≤ 30¢, 60–5 s left), first sample per window/side/g bucket:\n")
        print("| g bucket | windows | mean ask ¢ | mean FV ¢ | win | edge_net ¢ ± SE |\n|---|---|---|---|---|---|")
        GB = ((-9, -0.05, "g < −5¢"), (-0.05, 0.0, "−5–0¢"), (0.0, 0.05, "0–5¢"), (0.05, 0.10, "5–10¢"), (0.10, 0.20, "10–20¢"), (0.20, 9, "≥ 20¢"))
        for lo, hi, nm in GB:
            m = el & (g >= lo) & (g < hi)
            sel = np.flatnonzero(m)
            if not len(sel):
                continue
            fi = sel[C.first_per([D["e"][sel], D["side"][sel]], D["t"][sel])]
            s = C.stats(D, fi, fee)
            print(f"| {nm} | {s['nw']} | {100 * s['ask']:.1f} | {100 * np.mean(D['fvcl'][fi]):.1f} | {s['win']:.3f} | {pm(s['en'])} |")
        for gname, sp in (("all", None), ("strike gap toward the side (kgap > 0)", D["kgap"] > 0),
                          ("strike gap against the side (kgap ≤ 0)", D["kgap"] <= 0)):
            sp3 = None if sp is None else (D3["kgap"] > 0 if "toward" in gname else D3["kgap"] <= 0)
            r, r3 = evaluate(D, fee, split=sp), evaluate(D3, fee, split=sp3)
            print(f"\n### {asset.upper()} {tf}m, {gname}\n")
            print(HDR)
            ts = []
            for c in sorted(r):
                txt, t = line(c, r[c], r3.get(c))
                print(txt)
                if t == t:
                    ts.append(t)
                if qualifies(r[c], t):
                    cands.append(dict(market=f"{asset}{tf}", fee=fee, grid=gname, code=c, label=lab(c), t=t, edge=r[c]["en"][0],
                                      se=r[c]["en"][1], pess=r[c]["ep"][0], fw=r[c]["fw"], h1=r[c]["h1"][0], h2=r[c]["h2"][0]))
            ses = sorted(r[c]["en"][1] for c in r if r[c]["en"][1] == r[c]["en"][1])
            n2 = sum(abs(x) >= 2 for x in ts)
            summary.append((f"{asset}{tf}", gname, len(r), sum(x >= 2.5 for x in ts), sum(x >= 2 for x in ts), sum(x <= -2 for x in ts)))
            print(f"\nCells with orders: {len(r)}; t ≥ 2.5: {sum(x >= 2.5 for x in ts)}; |t| ≥ 2: {n2} (positive {sum(x >= 2 for x in ts)}, "
                  f"negative {sum(x <= -2 for x in ts)}; chance ≈ {0.0455 * len(ts):.1f}). Smallest detectable edge at 2.5 SE: "
                  f"median {250 * ses[len(ses) // 2]:.1f}¢, best {250 * ses[0]:.1f}¢." if ses else "\nNo cells.")
    print("\n## Summary over grids\n\n| market | grid | cells | t ≥ 2.5 | t ≥ 2 | t ≤ −2 |\n|---|---|---|---|---|---|")
    for s in summary:
        print("| " + " | ".join(str(x) for x in s) + " |")
    cands.sort(key=lambda x: -x["t"])
    print("\nQualifying cells (≥ 30 filled windows, edge_net > 0 at t ≥ 2.5, both halves > 0, pess > 0): " +
          ("; ".join(f"{c['market']} {c['grid']} {c['label']} t={c['t']:.2f}" for c in cands) or "none"))
    if outj:
        json.dump(dict(qualifying=cands, picked=cands[:2]), open(outj, "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
