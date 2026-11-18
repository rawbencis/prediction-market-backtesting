"""Tables for the live `bid` rule on 5m books (alt_bid.py output). Usage: python alt_report.py [dev|all] -> stdout markdown.
dev: BTC 11-14..17 calibration + all assets on 11-14..16. all: also the 11-17 holdout for the alts."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pmlat import boot_ci  # noqa: E402

D = Path(__file__).resolve().parent / "out" / "alts"
DEV = ("2026-11-14", "2026-11-15", "2026-11-16")
HO = ("2026-11-17",)
ASSETS = ("btc", "eth", "sol", "xrp", "doge")
BANDS = [(0, 2000, "<20"), (2000, 4000, "20-40"), (4000, 6000, "40-60"), (6000, 8000, "60-80"), (8000, 10001, ">80")]
LIVE_START, LIVE_END = 1794950160, 1795007340  # 2026-11-17 21:16 -> 2026-11-18 13:09 UTC
LIVE = dict(fills=414, shares=2039, settle=-0.80, settle_pm=2.35, wins=183, implied=185.4, mo1=-0.48, partial=0.24,
            onesided=0.33, unhedged=2.4)


def day_of(e):
    return datetime.fromtimestamp(int(e), timezone.utc).date().isoformat()


def load(asset, days):
    o, m = [], []
    for d in days:
        f = D / f"ab-{asset}5-{d}.csv.gz"
        if not f.exists():
            continue
        o.append(pd.read_csv(f))
        m.append(pd.read_csv(D / f"ab-{asset}5-{d}-mk.csv"))
    if not o:
        return None, None
    o, m = pd.concat(o, ignore_index=True), pd.concat(m, ignore_index=True)
    m = m.drop_duplicates("e")
    o["day"] = o["e"].map(day_of)
    m["day"] = m["e"].map(day_of)
    return o, m


def wavg(v, w):
    v, w = np.asarray(v, float), np.asarray(w, float)
    k = ~np.isnan(v)
    return float((v[k] * w[k]).sum() / w[k].sum()) if w[k].sum() > 0 else float("nan")


def fills_of(o):
    f = o[o["filled"] > 0].copy()
    f["pnl"] = f["settle_c"] * f["filled"]  # cents
    return f


def windows(o, m):
    """Per window: shares and P&L by token, all recorded windows (0 if no fill)."""
    f = fills_of(o)
    g = f.groupby(["e", "tok"]).agg(sh=("filled", "sum"), pnl=("pnl", "sum")).unstack(fill_value=0)
    g.columns = [f"{a}{b}" for a, b in g.columns]
    for c in ("sh0", "sh1", "pnl0", "pnl1"):
        if c not in g:
            g[c] = 0.0
    w = m[m["winner"].notna()][["e", "day"]].set_index("e").join(g, how="left").fillna(0.0)
    w["pnl"] = (w["pnl0"] + w["pnl1"]) / 100  # $ at 5-share orders
    w["sh"] = w["sh0"] + w["sh1"]
    w["kind"] = np.where(w["sh"] == 0, "none", np.where((w["sh0"] > 0) & (w["sh1"] > 0), "pair", "one"))
    w["unh"] = (w["sh0"] - w["sh1"]).abs()
    return w


def wins_z(f):
    p = f["px"].to_numpy() / 1e4
    won = (f["settle_c"] > 0).to_numpy().astype(float)
    W, I = won.sum(), p.sum()
    zn = (W - I) / np.sqrt((p * (1 - p)).sum())
    r = pd.Series(won - p).groupby(f["e"].to_numpy()).sum()
    zc = (W - I) / np.sqrt((r ** 2).sum())
    return W, I, zn, zc


def stats(o, m, ndays_eq):
    """One row of per-fill stats. ndays_eq = recorded windows / 288."""
    f = fills_of(o)
    f = f[f["settle_c"].notna()]
    ok = o[o["status"] == "ok"]
    if not len(f):
        return dict(orders_d=len(o) / ndays_eq, fills_d=0)
    s = f["pnl"].sum() / f["filled"].sum()
    cm = boot_ci(f["pnl"], f["e"], weights=f["filled"])
    cd = boot_ci(f["pnl"], f["day"], weights=f["filled"])
    W, I, zn, zc = wins_z(f)
    return dict(orders_d=len(o) / ndays_eq, fill_rate=len(f) / max(len(ok), 1), fills_d=len(f) / ndays_eq,
                shares_d=f["filled"].sum() / ndays_eq, settle=s, ci_m=cm, ci_d=cd, wins=W, implied=I, zn=zn, zc=zc,
                mo1=wavg(f["mo1000"], f["filled"]), mo5=wavg(f["mo5000"], f["filled"]),
                reb=wavg(f["rebate_c"], f["filled"]), spr=f["spr"].median() / 100, sz=f["lvl_sz"].median(),
                sz_arr=f["lvl_sz_arr"].median(), n=len(f), partial=(f["filled"] < 5 - 1e-9).mean(),
                multi=(f["n_ev"] > 1).mean())


def fmt_ci(c):
    return "n/a" if c is None or np.isnan(c[0]) else f"[{c[0]:+.2f}, {c[1]:+.2f}]"


def ftab(rows):
    h = ("| set | band | fills/day | shares/day | fill rate | settle ¢/sh | 90% CI market | 90% CI day | settle + rebate | "
         "wins / implied (z naive, z by market) | mo 1 s | mo 5 s | rebate ¢/sh | median spread ¢ | median size at join (at arrival) |")
    out = [h, "|" + "---|" * 15]
    for lab, band, r in rows:
        if "settle" not in r:
            out.append(f"| {lab} | {band} | 0 | | | | | | | | | | | | |")
            continue
        out.append(f"| {lab} | {band} | {r['fills_d']:,.0f} | {r['shares_d']:,.0f} | {r['fill_rate']:.2f} | {r['settle']:+.2f} | "
                   f"{fmt_ci(r['ci_m'])} | {fmt_ci(r['ci_d'])} | {r['settle'] + r['reb']:+.2f} | "
                   f"{r['wins']:.0f} / {r['implied']:.1f} ({r['zn']:+.1f}, {r['zc']:+.1f}) | {r['mo1']:+.2f} | {r['mo5']:+.2f} | "
                   f"{r['reb']:.2f} | {r['spr']:.0f} | {r['sz']:.0f} ({r['sz_arr']:.0f}) |")
    return "\n".join(out)


def wtab(rows):
    out = ["| set | windows/day | with a fill | pair | one-sided | pair P&L $/window (¢/sh) | one-sided P&L $/window (¢/sh) | "
           "unhedged sh/window (with a fill) | window P&L $ mean [90% CI] |", "|" + "---|" * 9]
    for lab, w, nd in rows:
        a = w[w["kind"] != "none"]
        p, s1 = a[a["kind"] == "pair"], a[a["kind"] == "one"]
        ci = boot_ci(w["pnl"], w.index.to_numpy())
        cps = lambda z: 100 * z["pnl"].sum() / z["sh"].sum() if z["sh"].sum() else float("nan")  # noqa: E731
        out.append(f"| {lab} | {len(w) / nd:.0f} | {len(a) / len(w):.2f} | {(a['kind'] == 'pair').mean():.2f} | "
                   f"{(a['kind'] == 'one').mean():.2f} | {p['pnl'].mean():+.3f} ({cps(p):+.2f}) | {s1['pnl'].mean():+.3f} ({cps(s1):+.2f}) | "
                   f"{a['unh'].mean():.1f} | {w['pnl'].mean():+.3f} {fmt_ci(ci)} |")
    return "\n".join(out)


def per_asset(asset, days_sets):
    rows, wrows = [], []
    for lab, days in days_sets:
        o, m = load(asset, days)
        if o is None:
            continue
        nd = len(m) / 288
        rows.append((lab, "all", stats(o, m, nd)))
        for lo, hi, bn in BANDS:
            rows.append((lab, bn, stats(o[(o["px"] >= lo) & (o["px"] < hi)], m, nd)))
        wrows.append((lab, windows(o, m), nd))
    return ftab(rows) + "\n\n" + wtab(wrows)


def calib():
    o, m = load("btc", DEV + HO)
    nd = len(m) / 288
    r = stats(o, m, nd)
    w = windows(o, m)
    a = w[w["kind"] != "none"]
    live_w = (LIVE_END - LIVE_START) / 300
    f = fills_of(o)
    se = np.sqrt(np.sum(((f.groupby("e")["pnl"].sum() - r["settle"] * f.groupby("e")["filled"].sum()) ** 2)) ) / f["filled"].sum()
    k = live_w / (len(m))
    sl = o[(o["e"] >= LIVE_START) & (o["e"] < LIVE_END)]
    out = ["| figure | live (11-17 21:16 → 11-18 13:09, 191 windows) | sim, BTC 5m 11-14..17 scaled to 191 windows | sim/live |",
           "|---|---|---|---|"]
    rowz = [("filled orders", LIVE["fills"], r["n"] * k), ("shares", LIVE["shares"], f["filled"].sum() * k),
            ("settled ¢/share", LIVE["settle"], r["settle"]),
            ("± (sim: 2 × market-clustered SE)", LIVE["settle_pm"], 2 * se),
            ("wins", LIVE["wins"], r["wins"] * k), ("implied by price", LIVE["implied"], r["implied"] * k),
            ("wins − implied per 414 fills", LIVE["wins"] - LIVE["implied"], (r["wins"] - r["implied"]) / r["n"] * 414),
            ("1 s markout ¢/share", LIVE["mo1"], r["mo1"]), ("partial fills (filled < 5)", LIVE["partial"], r["partial"]),
            ("windows ending one-sided (of windows with a fill)", LIVE["onesided"], (a["kind"] == "one").mean()),
            ("unhedged shares / window (windows with a fill)", LIVE["unhedged"], a["unh"].mean())]
    for name, lv, sv in rowz:
        rat = sv / lv if lv not in (0,) and np.sign(lv) == np.sign(sv) else float("nan")
        out.append(f"| {name} | {lv:,.2f} | {sv:,.2f} | {'' if np.isnan(rat) else f'{rat:.2f}'} |")
    extra = (f"\nSim extra: orders sent {len(o) / nd:,.0f}/day, fill rate per accepted order {r['fill_rate']:.2f}, "
             f"filled orders with > 1 fill event {r['multi']:.2f}, 5 s markout {r['mo5']:+.2f}, settle 90% CI market "
             f"{fmt_ci(r['ci_m'])}, wins z {r['zn']:+.1f} naive / {r['zc']:+.1f} by market, spread at join median {r['spr']:.0f}¢, "
             f"{len(m)} windows. Overlap with the live period in the recording (11-17 21:16-24:00): {len(sl)} orders sim.")
    return "\n".join(out) + extra


def paired_diff(j, B=2000, seed=1):
    """alt c/share minus BTC c/share over the same windows; 90% CI resampling windows (pairs kept together)."""
    pa, sa, pb, sb = (j[c].to_numpy() for c in ("pnl_a", "sh_a", "pnl_b", "sh_b"))
    i = np.random.default_rng(seed).integers(0, len(j), (B, len(j)))
    d = 100 * (pa[i].sum(1) / sa[i].sum(1) - pb[i].sum(1) / sb[i].sum(1))
    lo, hi = np.percentile(d, [5, 95])
    return lo, hi, 100 * (pa.sum() / sa.sum() - pb.sum() / sb.sum())


def corr_sd(days, lab):
    ob, mb = load("btc", days)
    wb = windows(ob, mb)
    out = [f"| {lab} | alt | windows both recorded | corr(window P&L) | corr, windows both traded (n) | daily SD $ BTC alone | "
           f"daily SD $ BTC + alt | daily SD $ alt alone | mean $/day BTC | mean $/day BTC + alt | alt − BTC settle ¢/sh, same windows [90% CI] |", "|" + "---|" * 11]
    for a in ASSETS[1:]:
        oa, ma = load(a, days)
        if oa is None:
            continue
        wa = windows(oa, ma)
        j = wb[["pnl", "sh"]].add_suffix("_b").join(wa[["pnl", "sh"]].add_suffix("_a"), how="inner")
        dlo, dhi, dmid = paired_diff(j)
        c = np.corrcoef(j["pnl_b"], j["pnl_a"])[0, 1]
        jt = j[(j["sh_b"] > 0) & (j["sh_a"] > 0)]
        ct = np.corrcoef(jt["pnl_b"], jt["pnl_a"])[0, 1] if len(jt) > 2 else float("nan")
        sd = lambda v: np.sqrt(288 * np.var(v, ddof=1))  # noqa: E731  (windows independent within a day)
        out.append(f"| {lab} | {a.upper()} | {len(j)} | {c:+.3f} | {ct:+.3f} ({len(jt)}) | {sd(j['pnl_b']):.2f} | "
                   f"{sd(j['pnl_b'] + j['pnl_a']):.2f} | {sd(j['pnl_a']):.2f} | {288 * j['pnl_b'].mean():+.2f} | "
                   f"{288 * (j['pnl_b'] + j['pnl_a']).mean():+.2f} | {dmid:+.2f} [{dlo:+.2f}, {dhi:+.2f}] |")
    return "\n".join(out)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dev"
    sets = [("dev", DEV)] + ([("holdout", HO)] if mode == "all" else [])
    print("## Calibration (BTC 5m)\n")
    print(calib())
    for a in ASSETS:
        print(f"\n### {a.upper()} 5m\n")
        print(per_asset(a, sets))
    print("\n## BTC vs alt, window P&L ($, 5-share orders)\n")
    for lab, days in sets:
        print(corr_sd(days, lab))
        print()


if __name__ == "__main__":
    main()
