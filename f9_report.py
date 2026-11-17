"""F9 tables: per config x split: orders/day, fill rate, 1 s / 5 s markout, settle c/share with 90% market CI, $/day,
pair cost. Rank rule: frozen/F9_rule.json. Usage: python f9_report.py SPLIT(dev|holdout) [TAG] -> markdown on stdout"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat

D = {"dev": ("2026-11-14", "2026-11-15"), "holdout": ("2026-11-16",)}
ITEM = {"alt": "2", "hedge": "3", "pair": "4", "pre": "5", "calm": "6", "imb": "8"}


def load(split, tag=""):
    F, O, P = [], [], []
    for tf, d, tg in [(tf, d, tg) for tf in (5, 15) for d in D[split] for tg in (tag, tag + "-i8", tag + "-i3")]:
        if True:
            b = pmlat.OUT / f"f9-live-{tf}m-{d}{tg}"
            if not Path(f"{b}.csv.gz").exists():
                continue
            f = pd.read_csv(f"{b}.csv.gz")
            o = pd.read_csv(f"{b}-orders.csv")
            p = pd.read_csv(f"{b}-pairs.csv")
            for z in (f, o, p):
                z["tf"], z["day"] = tf, d
            F.append(f), O.append(o), P.append(p)
    cat = lambda z: pd.concat(z) if z else pd.DataFrame()  # noqa: E731
    return cat(F), cat(O), cat(P)


def table(F, O, P, ndays):
    rows = []
    O = O.groupby(["tf", "cfg"])[["orders", "accepted", "filled_orders"]].sum().reset_index()
    for r in O.itertuples():
        if r.cfg.endswith("#hedge"):
            continue
        base = r.cfg
        f = F[(F.tf == r.tf) & (F.cfg == base)]
        hed = O[(O.tf == r.tf) & (O.cfg == base + "#hedge")]
        orders = r.orders + (int(hed.orders.sum()) if len(hed) else 0)
        filled = r.filled_orders + (int(hed.filled_orders.sum()) if len(hed) else 0)
        item = ("7" if r.tf == 15 else ITEM[base.split("|")[0]])
        row = dict(item=item, tf=r.tf, cfg=base, orders_day=orders / ndays, fill_rate=filled / max(orders, 1), fills=len(f))
        if len(f):
            w = f.filled.values
            sw = lambda c: float((f[c] * w).sum() / w[f[c].notna().values].sum()) if f[c].notna().any() else np.nan  # noqa: E731
            row.update(mo1s=sw("mo1000"), mo5s=sw("mo5000"))
            s = f[f.settle_c.notna()]
            row["settle_c"] = float((s.settle_c * s.filled).sum() / s.filled.sum())
            lo, hi = pmlat.boot_ci(s.settle_c * s.filled, s.day + s.e.astype(str), weights=s.filled)
            row.update(ci_lo=lo, ci_hi=hi, usd_day=float((s.settle_c * s.filled).sum() / 100 / ndays))
            # pair cost: avg UP px + avg DOWN px per window, over windows where both tokens filled
            g = f.groupby(["day", "e", "tok"]).apply(lambda z: (z.px * z.filled).sum() / z.filled.sum(), include_groups=False).unstack()
            both = g.dropna() if g.shape[1] == 2 else g.iloc[0:0]
            row["pair_cost"] = float((both[0] + both[1]).mean() / 1e4) if len(both) else np.nan
            row["pair_n"] = f"{len(both)}/{len(g)}"
            if base.startswith("pair"):
                p = P[(P.tf == r.tf) & (P.cfg == base)]
                pf = p[p.legs_filled > 0]
                row["both_legs"] = f"{(pf.legs_filled == 2).mean():.2f} of filled pairs ({(p.legs_filled == 2).mean():.2f} of all)"
                one = f[f.both == 0]
                row["one_leg_settle_c"] = float((one.settle_c * one.filled).sum() / one.filled.sum()) if len(one) else np.nan
                row["one_leg_usd_day"] = float((one.settle_c * one.filled).sum() / 100 / ndays) if len(one) else np.nan
            if base.startswith("hedge"):
                row["hedge_fills"] = int((f.role == "hedge").sum())
        rows.append(row)
    return pd.DataFrame(rows)


def bands(F, ndays):
    """Item 8 rows by token price band and by time left at fill."""
    f = F[F.cfg.str.startswith("imb") & F.settle_c.notna()].copy()
    if not len(f):
        return ""
    f["band"] = pd.cut(f.px / 1e4, [0, 0.3, 0.5, 0.7, 1.0], labels=["<0.30", "0.30-0.50", "0.50-0.70", ">=0.70"], right=False)
    f["left"] = pd.cut(f.tleft / 1000, [0, 60, 120, 240, 1e9], labels=["<60s", "60-120s", "120-240s", ">=240s"], right=False)
    out = []
    for by in ("band", "left"):
        rows = []
        for (cfg, g), z in f.groupby(["cfg", by], observed=True):
            w = z.filled
            lo, hi = pmlat.boot_ci(z.settle_c * w, z.day + z.e.astype(str), weights=w)
            mo = z[z.mo1000.notna()]
            rows.append(dict(cfg=cfg, **{by: str(g)}, fills=len(z), settle_c=float((z.settle_c * w).sum() / w.sum()), ci_lo=lo, ci_hi=hi,
                             mo1s=float((mo.mo1000 * mo.filled).sum() / mo.filled.sum()) if len(mo) else np.nan,
                             usd_day=float((z.settle_c * w).sum() / 100 / ndays)))
        out.append(md(pd.DataFrame(rows), ["cfg", by, "fills", "settle_c", "ci_lo", "ci_hi", "mo1s", "usd_day"]))
    return "\n\n".join(out)


def rank(T):
    E = T[(T.fills >= 200) & T.ci_lo.notna()].copy()
    return E.sort_values(["ci_lo", "mo1s"], ascending=[False, False])


def md(T, cols):
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in T[cols].itertuples(index=False):
        out.append("| " + " | ".join(f"{v:+.2f}" if isinstance(v, float) and c not in ("orders_day", "fill_rate", "pair_cost") else
                                     f"{v:,.0f}" if c == "orders_day" else f"{v:.3f}" if isinstance(v, float) else str(v)
                                     for c, v in zip(cols, r)) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    split = sys.argv[1]
    tag = sys.argv[2] if len(sys.argv) > 2 else ""
    F, O, P = load(split, tag)
    T = table(F, O, P, len(D[split]))
    T.to_csv(pmlat.OUT / f"f9-table-{split}{tag}.csv", index=False)
    cols = ["item", "cfg", "orders_day", "fill_rate", "fills", "mo1s", "mo5s", "settle_c", "ci_lo", "ci_hi", "usd_day", "pair_cost", "pair_n"]
    print(md(T.sort_values(["item", "cfg"]), cols))
    if "both_legs" in T:
        X = T[T.cfg.str.startswith("pair")]
        print("\n" + md(X, ["cfg", "both_legs", "one_leg_settle_c", "one_leg_usd_day"]))
    b8 = bands(F, len(D[split]))
    if b8:
        print("\nitem 8 by token price band / time left at fill:\n" + b8)
    if split == "dev":
        print("\nrank (rule: CI lower bound, >= 200 fills):\n" + md(rank(T).head(8), ["item", "cfg", "fills", "settle_c", "ci_lo", "mo1s"]))
