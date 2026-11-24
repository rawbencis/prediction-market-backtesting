"""D1-b: one more wallet (0xc387c2a4, @flippingsharks), same method as D1 (act_rec.py fills --no-binance, per day).
All ten days 11-14..23 are out of sample (the wallet was not selected on our data).
Per set: fills, markets, $, settled P&L recomputed at the fee in force (taker 0.07 BTC before 11-20 23:59, 0.05 after;
alts 0.01; maker rebate 0), c/$ with a 90% CI resampling markets, by day; 1 s / 5 s token-mid markouts of maker fills
from the shadow-rec top book (fill exchange ms + 1 s / 5 s, book two-sided, > 30 s left at the markout time; c/share,
share-weighted, signed for the fill side, CI by market); pairs (share of bought shares paired UP+DOWN in the same market,
average pair cost). Then the 11-19 ladder classes (mk_level_w.py output, BTC only: pmbook has no alts).
usage: d1b_report.py -> markdown"""
import os, pickle, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

D = Path.home() / "dublin-1124" / "d1b"
REC = Path.home() / "shadow-rec"
FEECUT = 1795219140_000
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
ASSETS = {"btc": 0, "eth": 1}
sys.path.insert(0, str(Path.home() / "overnight-wt"))


def load(asset, tf):
    rows = []
    for f in sorted(D.glob(f"c387_{asset}_{tf}_*.pkl")):
        rows += pickle.load(open(f, "rb"))
    for r in rows:
        r["asset"], r["tf"] = asset, tf
    return rows


def pnl2(r):
    win = 1.0 if r["out"] == "W" else 0.0
    fr = 0.0 if r["role"] == "maker" else (0.01 if r["asset"] != "btc" else (0.07 if r["x"] < FEECUT else 0.05))
    return ((win - r["p"]) if r["side"] == "BUY" else (r["p"] - win)) * r["sz"] - fr * r["p"] * (1 - r["p"]) * r["sz"]


def markouts(rows):
    """mo1/mo5 (c/share, signed) per maker fill, streaming one hour of pm rows at a time."""
    by = {}
    for r in rows:
        by.setdefault(time.strftime("%Y-%m-%d %H", time.gmtime(r["x"] / 1000)), []).append(r)
    for hk, rs in sorted(by.items()):
        t0 = time.mktime(time.strptime(hk, "%Y-%m-%d %H")) - time.timezone
        parts = []
        for t in (t0, t0 + 3600):
            d, h = time.strftime("%Y-%m-%d", time.gmtime(t)), time.gmtime(t).tm_hour
            f = REC / d / f"pm-{h:02d}.bin"
            if f.exists():
                x = np.fromfile(f, dtype=PM_DT)
                eps = np.array(sorted({int(r["key"].rsplit("-", 1)[1]) for r in rs}), np.uint32)
                parts.append(x[np.isin(x["e"], eps)])
        pm = np.concatenate(parts) if parts else np.zeros(0, PM_DT)
        groups = {}
        for r in rs:
            e = int(r["key"].rsplit("-", 1)[1])
            k = (ASSETS[r["asset"]], r["tf"], e)
            if k not in groups:
                y = pm[(pm["a"] == k[0]) & (pm["tf"] == k[1]) & (pm["e"] == e)]
                groups[k] = y[np.argsort(y["x"], kind="stable")]
            y = groups[k]
            end = (e + 60 * r["tf"]) * 1000
            sg = 1 if r["side"] == "BUY" else -1
            for d in (1000, 5000):
                t = r["x"] + d
                j = np.searchsorted(y["x"], t, "right") - 1
                ok = end - t > 30000 and j >= 0 and 0 < y["b"][j] < y["k"][j] < 10000
                if ok:
                    mu = (int(y["b"][j]) + int(y["k"][j])) / 2e4
                    mu = mu if r["tok"] == "UP" else 1 - mu
                r[f"mo{d // 1000}"] = 100 * sg * (mu - r["p"]) if ok else np.nan


def ci_ratio(num, den, g, B=2000):
    uk, inv = np.unique(g, return_inverse=True)
    if len(uk) < 2:
        return np.nan, np.nan
    S, N = np.bincount(inv, num), np.bincount(inv, den)
    i = np.random.default_rng(0).integers(0, len(uk), (B, len(uk)))
    with np.errstate(invalid="ignore", divide="ignore"):
        return tuple(np.nanpercentile(S[i].sum(1) / N[i].sum(1), [5, 95]))


def stat(rows):
    if not rows:
        return None
    pnl = np.array([r["pnl2"] for r in rows]); usd = np.array([r["usd"] for r in rows]); g = [r["key"] for r in rows]
    lo, hi = ci_ratio(100 * pnl, usd, g)
    buys = [r for r in rows if r["side"] == "BUY"]
    return dict(n=len(rows), mk=len(set(g)), usd=usd.sum(), pnl=pnl.sum(), c=100 * pnl.sum() / usd.sum(), lo=lo, hi=hi,
                win=np.mean([r["out"] == "W" for r in buys]) if buys else np.nan,
                maker=np.mean([r["role"] == "maker" for r in rows]), buy=len(buys) / len(rows))


def mo_stat(rows, k):
    v = np.array([r.get(k, np.nan) for r in rows]); w = np.array([r["sz"] for r in rows]); ok = np.isfinite(v)
    if ok.sum() < 2:
        return "n/a"
    lo, hi = ci_ratio(v[ok] * w[ok], w[ok], [r["key"] for r, o in zip(rows, ok) if o])
    return f"{(v[ok] * w[ok]).sum() / w[ok].sum():+.2f} [{lo:+.2f}, {hi:+.2f}] (n {ok.sum():,})"


def pairs(rows):
    by = {}
    for r in rows:
        if r["side"] != "BUY":
            continue
        d = by.setdefault(r["key"], {"UP": [0.0, 0.0], "DOWN": [0.0, 0.0]})
        d[r["tok"]][0] += r["sz"]; d[r["tok"]][1] += r["usd"]
    both = [d for d in by.values() if d["UP"][0] > 0 and d["DOWN"][0] > 0]
    paired = sum(min(d["UP"][0], d["DOWN"][0]) for d in both)
    cost = sum(min(d["UP"][0], d["DOWN"][0]) * (d["UP"][1] / d["UP"][0] + d["DOWN"][1] / d["DOWN"][0]) for d in both)
    shares = sum(d["UP"][0] + d["DOWN"][0] for d in by.values())
    return len(by), len(both), 2 * paired / max(shares, 1e-9), cost / max(paired, 1e-9), paired - cost


H = ("| set | fills | markets | $ | P&L $ | ¢/$ [90% CI by market] | win rate (buys) | maker share | buy share | "
     "maker mo 1 s ¢/sh [90% CI] | maker mo 5 s ¢/sh [90% CI] | markets, both sides bought | shares paired | pair cost | P&L locked in pairs $ |\n"
     + "|---" * 15 + "|")


def line(name, rows):
    s = stat(rows)
    if s is None:
        return f"| {name} | 0 |" + " |" * 13
    mk = [r for r in rows if r["role"] == "maker"]
    nm, nb, sh, pc, lk = pairs(rows)
    return (f"| {name} | {s['n']:,} | {s['mk']:,} | {s['usd']:,.0f} | {s['pnl']:+,.0f} | {s['c']:+.2f} [{s['lo']:+.2f}, {s['hi']:+.2f}] | "
            f"{s['win']:.3f} | {s['maker']:.0%} | {s['buy']:.0%} | {mo_stat(mk, 'mo1')} | {mo_stat(mk, 'mo5')} | "
            f"{nb:,} of {nm:,} | {sh:.0%} | {pc:.3f} | {lk:+,.0f} |")


def main():
    sets = {}
    for asset, tf in (("btc", 5), ("btc", 15), ("eth", 5)):
        rows = [r for r in load(asset, tf) if r["out"] in ("W", "L")]
        for r in rows:
            r["pnl2"] = pnl2(r)
        markouts(rows)
        sets[f"{asset.upper()} {tf}m"] = rows
        print(f"{asset} {tf}: {len(rows)} settled fills", file=sys.stderr, flush=True)
    allr = [r for v in sets.values() for r in v]
    print(H)
    for k, v in sets.items():
        print(line(k, v))
    print(line("**all**", allr))
    print(line("all, 11-14..18", [r for r in allr if r["x"] < 1795046400_000]))
    print(line("all, 11-19..23", [r for r in allr if r["x"] >= 1795046400_000]))
    print(line("all, BUY up to 50¢", [r for r in allr if r["side"] == "BUY" and r["p"] <= 0.5]))
    print(line("all, BUY over 50¢", [r for r in allr if r["side"] == "BUY" and r["p"] > 0.5]))
    for k, v in list(sets.items()) + [("all", allr)]:
        days = {}
        for r in v:
            days.setdefault(time.strftime("%m-%d", time.gmtime(r["x"] / 1000)), []).append(r)
        print(f"\n{k} by day (¢/$ [90% CI by market], $ P&L, fills): " + "; ".join(
            f"{d} {s['c']:+.1f} [{s['lo']:+.1f}, {s['hi']:+.1f}] {s['pnl']:+,.0f}$ n={s['n']}" for d, s in ((d, stat(x)) for d, x in sorted(days.items()))))
    # ladder classes (BTC only)
    import lvl_report
    lvl_report.WS = ("c387c2a40d",)
    for tf in (5, 15):
        fs = sorted((D / "lvl").glob(f"lvl-{tf}-*.csv.gz"))
        if not fs:
            continue
        L = pd.concat([pd.read_csv(f, low_memory=False) for f in fs], ignore_index=True)
        if "nobook" not in L:
            L["nobook"] = np.nan
        nob = L.nobook.notna().sum()
        L = L[L.nobook.isna()]
        os.environ["LVL_TF"], os.environ["LVL_W"] = str(tf), "0xc387c2a40d389f17b723b6bba9b18b7dbd2de4f4"
        from dublin1124 import mk_level_w
        mk_level_w.TF = tf
        nb = L.e.map(mk_level_w.bad_window)
        print(f"\n#### Ladder classes, BTC {tf}m ({fs[0].name[-17:-7]}..{fs[-1].name[-17:-7]}; {nob} fills with no pmbook rows and "
              f"{int(nb.sum())} fills in {L.e[nb].nunique()} windows overlapping a corrupt pmbook hour excluded)")
        L = L[~nb]
        print(lvl_report.tick_table(L))
        print(lvl_report.step1(L))


if __name__ == "__main__":
    main()
