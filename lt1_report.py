"""LT1 dev / holdout tables from lt1_sim.py outputs (frozen/LT1_plan.json). One market at a time (memory).
Usage: python lt1_report.py dev|holdout [--pick frozen/LT1_pick.json] -> reports/dublin/lt1/{phase}_*.csv + stdout
"""
import json
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import lt1_sim as L

DEV = [f"2026-11-{d}" for d in range(14, 24)]
HOLD = ["2026-11-25", "2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01"]
MODELS = ["R", "S1", "S2", "M15", "M60", "EMA"]
XE = [(x, ex) for x in L.XS for ex in (1, 2)]
NB = 2000
OUTD = L.HERE / "reports" / "dublin" / "lt1"


def day_of(e):
    return datetime.fromtimestamp(int(e), timezone.utc).strftime("%Y-%m-%d")


def load(days, tf):
    cols = ["e", "tok", "pull", "q", "step", "off", "rung", "px", "fsz", "mo1", "mo5", "mo30"]
    for x in L.XS:
        cols += [f"tp{x}", f"tps1_{x}", f"tps2_{x}", f"pnl1_{x}", f"pnl2_{x}"]
    parts = []
    for d in days:
        z = pd.read_csv(L.OUT / f"lt1-{d}-{tf}m.csv.gz", usecols=cols)
        for c in z.columns:
            if z[c].dtype == np.float64:
                z[c] = z[c].astype(np.float32)
        parts.append(z)
    return pd.concat(parts, ignore_index=True)


def sides(days, tf):
    s = pd.concat([pd.read_csv(L.OUT / f"sides-{d}.csv") for d in days])
    return s[s.tf == tf].sort_values("e").reset_index(drop=True)


def agg(d):
    """Per (e, tok) sums for one ladder (rows already filtered)."""
    f = d.fsz.astype(float)
    g = pd.DataFrame({"e": d.e, "tok": d.tok, "S": f, "R": 1.0})
    for h in (1, 5, 30):
        v = d[f"mo{h}"].astype(float)
        g[f"m{h}"] = np.where(v.notna(), v * f, 0.0)
        g[f"n{h}"] = np.where(v.notna(), f, 0.0)
    for x, ex in XE:
        s_tp = d[f"tps{ex}_{x}"].astype(float)
        g[f"P{x}{ex}"] = d[f"pnl{ex}_{x}"].astype(float)
        g[f"T{x}{ex}"] = s_tp
        g[f"Q{x}{ex}"] = s_tp * (d[f"tp{x}"].astype(float) - d.px.astype(float)) / 1e4  # TP profit $
    a = g.groupby(["e", "tok"]).sum()
    a["R"] = d.groupby(["e", "tok"]).rung.nunique()  # filled rungs
    return a


def boot_w(n, seed=0):
    rng = np.random.default_rng(seed)
    return np.stack([np.bincount(rng.integers(0, n, n), minlength=n) for _ in range(NB)]).astype(np.float32)


def cells(d, sd, Wt, q, phase):
    """All (ladder, K, model, x, exit) cells for one market and queue model. -> DataFrame, mirror-diff DataFrame."""
    eidx = {e: i for i, e in enumerate(sd.e.values)}
    nW = len(sd)
    days = sd.e.map(day_of).values
    udays, dinv = np.unique(days, return_inverse=True)
    side = {m: sd[m].values for m in MODELS}
    out, mir = [], []
    dq = d[d.q == q]
    for (step, off, pull), g0 in dq.groupby(["step", "off", "pull"]):
        for K in (1, 3, 5):
            a = agg(g0[g0.rung < K])
            cols = list(a.columns)
            V = np.zeros((nW, 2, len(cols)))
            ii = np.array([eidx.get(e, -1) for e in a.index.get_level_values(0)])
            tk = a.index.get_level_values(1).values
            ok = ii >= 0
            V[ii[ok], tk[ok]] = a.values[ok]
            sel = {}
            for m in MODELS:
                s = side[m]
                has = s >= 0
                v = np.zeros((nW, len(cols)))
                v[has] = V[np.flatnonzero(has), s[has]]
                sel[m] = v
                vm = np.zeros((nW, len(cols)))
                vm[has] = V[np.flatnonzero(has), 1 - s[has]]
                sel[m + "_mir"] = vm
            sel["BOTH"] = V.sum(1)
            ci = {c: j for j, c in enumerate(cols)}
            names = list(sel)
            M = np.stack([sel[n] for n in names])  # model x window x col
            # bootstrap sums: B x model x col
            Bs = np.einsum("bw,mwc->bmc", Wt, M.astype(np.float32), optimize=True)
            tot = M.sum(1)
            dayP = np.stack([np.bincount(dinv, M[:, :, ci[f"P{x}{ex}"]][k], len(udays)) for k in range(len(names)) for x, ex in XE])
            for k, n in enumerate(names):
                S, R = tot[k, ci["S"]], tot[k, ci["R"]]
                for j, (x, ex) in enumerate(XE):
                    P, T, Qp = tot[k, ci[f"P{x}{ex}"]], tot[k, ci[f"T{x}{ex}"]], tot[k, ci[f"Q{x}{ex}"]]
                    with np.errstate(invalid="ignore", divide="ignore"):
                        bt = 100 * Bs[:, k, ci[f"P{x}{ex}"]] / Bs[:, k, ci["S"]]
                    lo, hi = np.nanpercentile(bt, [5, 95]) if S > 0 else (np.nan, np.nan)
                    dp = dayP[k * len(XE) + j]
                    pw = M[k, :, ci[f"P{x}{ex}"]]
                    out.append(dict(tf=None, q=q, model=n, step=step, off=off, pull=pull, K=K, x=x, exit=f"E{ex}",
                                    fills_day=R / len(udays), c_share=100 * P / S if S else np.nan, lo90=lo, hi90=hi,
                                    usd_day=P / len(udays), tp_hit=T / S if S else np.nan,
                                    nontp_c=100 * (P - Qp) / (S - T) if S - T > 1e-9 else np.nan,
                                    mo1=tot[k, ci["m1"]] / tot[k, ci["n1"]] if tot[k, ci["n1"]] else np.nan,
                                    mo5=tot[k, ci["m5"]] / tot[k, ci["n5"]] if tot[k, ci["n5"]] else np.nan,
                                    mo30=tot[k, ci["m30"]] / tot[k, ci["n30"]] if tot[k, ci["n30"]] else np.nan,
                                    worst_window=pw.min(), worst_day=dp.min(), days_pos=int((dp > 0).sum()), n_days=len(udays),
                                    rungs=R, shares=S))
            for m in MODELS:
                k1, k2 = names.index(m), names.index(m + "_mir")
                for x, ex in XE:
                    c1, c2 = ci[f"P{x}{ex}"], ci["S"]
                    with np.errstate(invalid="ignore", divide="ignore"):
                        bt = 100 * (Bs[:, k1, c1] / Bs[:, k1, c2] - Bs[:, k2, c1] / Bs[:, k2, c2])
                    pt = 100 * (tot[k1, c1] / tot[k1, c2] - tot[k2, c1] / tot[k2, c2])
                    lo, hi = np.nanpercentile(bt, [5, 95])
                    mir.append(dict(q=q, model=m, step=step, off=off, pull=pull, K=K, x=x, exit=f"E{ex}", diff=pt, lo90=lo, hi90=hi))
    return pd.DataFrame(out), pd.DataFrame(mir)


def mechanism(d, q="pess"):
    """Markouts by pull and rung depth below the anchor (c), both tokens, all ladders with K=5."""
    z = d[d.q == q].copy()
    z["depth"] = z.off + z.rung * z.step
    z["dbin"] = pd.cut(z.depth, [-1, 0, 1, 2, 4, 8, 20], labels=["0", "1", "2", "3-4", "5-8", "9+"])
    rows = []
    for (pull, db), g in z.groupby(["pull", "dbin"], observed=True):
        f = g.fsz.astype(float)
        r = dict(pull=pull, depth_c=db, fill_events=len(g), shares=f.sum())
        for h in (1, 5, 30):
            v = g[f"mo{h}"].astype(float)
            r[f"mo{h}"] = (v * f)[v.notna()].sum() / f[v.notna()].sum()
        rows.append(r)
    return pd.DataFrame(rows)


def v3(d, q="pess"):
    z = d[(d.q == q) & (d.pull == "none") & (d.off == 0) & (d.rung == 0) & (d.step == 1)]
    f = z.fsz.astype(float)
    out = {"fill_events": len(z), "rungs": z.groupby(["e", "tok"]).ngroups,
           "mo1": float((z.mo1 * f)[z.mo1.notna()].sum() / f[z.mo1.notna()].sum())}
    pw = (z.mo1 * f).groupby(z.e).sum()
    sw = f[z.mo1.notna()].groupby(z.e[z.mo1.notna()]).sum()
    pw = pw.reindex(sw.index)
    rng = np.random.default_rng(0)
    bt = [pw.values[j].sum() / sw.values[j].sum() for j in (rng.integers(0, len(pw), len(pw)) for _ in range(NB))]
    out["lo90"], out["hi90"] = np.percentile(bt, [5, 95])
    out["by_day"] = {dd: round(float((g.mo1 * g.fsz)[g.mo1.notna()].sum() / g.fsz[g.mo1.notna()].sum()), 3)
                     for dd, g in z.groupby(z.e.map(day_of))}
    return out


def main():
    phase = sys.argv[1]
    days = DEV if phase == "dev" else HOLD
    OUTD.mkdir(parents=True, exist_ok=True)
    allc, allm = [], []
    for tf in (5, 15):
        d = load(days, tf)
        sd = sides(days, tf)
        Wt = boot_w(len(sd))
        if phase == "dev":
            print(f"V3 {tf}m pess:", json.dumps(v3(d), default=float))
            print(f"V3 {tf}m prop:", json.dumps(v3(d, "prop"), default=float))
            mech = mechanism(d)
            mech.insert(0, "tf", tf)
            mech.to_csv(OUTD / f"{phase}_mechanism_{tf}m.csv", index=False)
            print(mech.round(3).to_string(index=False))
        for q in ("pess", "prop"):
            c, m = cells(d, sd, Wt, q, phase)
            c["tf"], m["tf"] = tf, tf
            allc.append(c)
            allm.append(m)
            print(tf, q, len(c), "cells", flush=True)
        del d
    C = pd.concat(allc, ignore_index=True)
    Mm = pd.concat(allm, ignore_index=True)
    C.to_csv(OUTD / f"{phase}_cells.csv.gz", index=False)
    Mm.to_csv(OUTD / f"{phase}_mirror.csv.gz", index=False)
    print("wrote", OUTD, len(C), "cells")


if __name__ == "__main__":
    main()
