"""Tables for steps 0-1 of the 15m ladder follow-up (mk_level.py output). Usage: python lvl_report.py -> stdout markdown."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pmlat import boot_ci  # noqa: E402

D0 = Path(__file__).resolve().parent / "out" / "lvl"
DAYS = ("2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18")
WS = ("239e726f1f", "e29042f5d9")


def load():
    D = pd.concat([pd.read_csv(D0 / f"lvl-{d}.csv.gz", low_memory=False) for d in DAYS if (D0 / f"lvl-{d}.csv.gz").exists()],
                  ignore_index=True)
    if "nobook" not in D:
        D["nobook"] = np.nan
    return D


def q(s, ps=(0.1, 0.5, 0.9)):
    s = pd.Series(s).dropna()
    return "/".join(f"{v:+.0f}" for v in s.quantile(ps)) + f" (n {len(s):,})" if len(s) else "n/a"


def wavg(v, w):
    v, w = np.asarray(v, float), np.asarray(w, float)
    k = ~np.isnan(v)
    return float((v[k] * w[k]).sum() / w[k].sum()) if w[k].sum() > 0 else float("nan")


def step0(D):
    out = ["| wallet | maker BUY fills | with a shadow-rec print (same tx) | print at the same token/price | same size | "
           "census emit − print ms p10/p50/p90 | census trade ts − print ms | pmbook print − shadow-rec print ms | "
           "pmbook book change − print ms | shadow-rec top-book change − print ms | fills with a pmbook book change found |",
           "|" + "---|" * 11]
    for w in WS:
        d = D[D.w == w]
        has = d.sh_print_x.notna()
        out.append(f"| {w}… | {len(d):,} | {has.mean():.3f} | {d.sh_print_exact[has].mean():.3f} | {d.sh_print_szeq[has].mean():.3f} | "
                   f"{q(d.cx - d.sh_print_x)} | {q(d.cts * 1000 - d.sh_print_x)} | {q(d.pm_print_x - d.sh_print_x)} | "
                   f"{q(d.book_x - d.pm_print_x)} | {q(d.sh_book_x - d.sh_print_x)} | {d.matched.fillna(False).astype(bool).mean():.3f} |")
    return "\n".join(out)


def tick_table(D):
    out = []
    for w in WS:
        d = D[(D.w == w) & D.new_tick.notna()]
        out.append(f"\n**{w}…** (maker BUY fills with a pmbook book; share of shares; ¢/share)\n")
        out.append("| vs best bid (ticks) | old: shadow-rec top book at print − 1 ms | "
                   "new: pmbook row before the fill's book change | new: mo 1 s | new: mo 5 s | new: settle |")
        out.append("|---|---|---|---|---|---|")
        bk = lambda s: s.clip(-2, 2)  # noqa: E731
        tot = d.sz.sum()
        for t, lab in ((-2, "≤ −2 (behind)"), (-1, "−1 (behind)"), (0, "0 (at the best bid)"), (1, "+1 (inside)"), (2, "≥ +2 (inside)")):
            o = d[bk(d.old_tick) == t].sz.sum() / d[d.old_tick.notna()].sz.sum()
            g = d[bk(d.new_tick) == t]
            out.append(f"| {lab} | {o:.3f} | {g.sz.sum() / tot:.3f} | {wavg(g.mo1000, g.sz):+.2f} | {wavg(g.mo5000, g.sz):+.2f} | "
                       f"{wavg(g.settle_c, g.sz):+.2f} |")
    return "\n".join(out)


def step1(D):
    out = []
    for w in WS:
        d = D[(D.w == w) & D.cls.notna()].copy()
        d["pnl"] = d.settle_c * d.sz
        out.append(f"\n**{w}…** ({len(d):,} maker BUY fills, {d.e.nunique()} markets; ¢/share, share-weighted; CI 90% by market)\n")
        out.append("| class | fills | share of fills | share of shares | mo 1 s | mo 5 s | mo 30 s | settle | 90% CI | in a sweep | "
                   "P best at the fill | time P had been best ms p50 (p10/p90) | volume printed at P since best p50 | "
                   "size at P when it became best p50 | size at P / dist ¢ at −5 s p50 | at −30 s | at −60 s | P visible at −30 s |")
        out.append("|" + "---|" * 18)
        for c in ("DEEP", "NEW", "OTHER", "all"):
            g = d if c == "all" else d[d.cls == c]
            if not len(g):
                continue
            ci = boot_ci(g.pnl.fillna(0), g.e, weights=g.sz)
            bst = g[g.best_ms.notna()]
            hist = []
            for h in (5, 30, 60):
                vis = g[g[f"st{h}"].isin(["best", "below"])]
                hist.append(f"{vis[f'z{h}'].median():.0f} / {vis[f'd{h}'].median():.0f}" if len(vis) else "n/a")
            v30 = g.st30.isin(["best", "below"]).mean()
            out.append(f"| {c} | {len(g):,} | {len(g) / len(d):.2f} | {g.sz.sum() / d.sz.sum():.2f} | {wavg(g.mo1000, g.sz):+.2f} | "
                       f"{wavg(g.mo5000, g.sz):+.2f} | {wavg(g.mo30000, g.sz):+.2f} | {wavg(g.settle_c, g.sz):+.2f} | "
                       f"[{ci[0]:+.2f}, {ci[1]:+.2f}] | {g.sweep.fillna(False).astype(bool).mean():.2f} | {len(bst) / len(g):.2f} | "
                       f"{bst.best_ms.median():,.0f} ({bst.best_ms.quantile(.1):,.0f}/{bst.best_ms.quantile(.9):,.0f}) | "
                       f"{bst.vol_since_best.median():,.0f} | {bst.sz_became_best.median():,.0f} | {hist[0]} | {hist[1]} | {hist[2]} | {v30:.2f} |")
        # where the -30 s state was
        st = d.groupby("cls").st30.value_counts(normalize=True).unstack(fill_value=0).round(2)
        out.append("\nState of level P 30 s before the fill (share of fills): best = P was the best bid; below = displayed below the best; "
                   "hidden = below the 5 shown levels; absent = inside the shown range with no size; above = above the best bid then.\n")
        cols = list(st.columns)
        out.append("| class | " + " | ".join(cols) + " |")
        out.append("|" + "---|" * (len(cols) + 1))
        for c, r in st.iterrows():
            out.append(f"| {c} | " + " | ".join(f"{v:.2f}" for v in r) + " |")
    return "\n".join(out)


def main():
    D = load()
    D = D[D.nobook.isna()]
    from mk_level import bad_window
    nb = D.e.map(bad_window)
    print(f"_Excluded: {int(nb.sum())} fills in {D.e[nb].nunique()} windows that overlap a corrupt pmbook hour file._\n")
    D = D[~nb]
    print("## Step 0\n")
    print(step0(D))
    print(tick_table(D))
    print("\n## Step 1\n")
    print(step1(D))


if __name__ == "__main__":
    main()
