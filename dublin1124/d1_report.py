import glob, pickle, sys, time
import numpy as np
from pathlib import Path
D = Path.home() / "dublin-1124/d1"
SPLIT = 1794834000_000  # 2026-11-16 13:00 UTC
FEECUT = 1795219140_000  # 2026-11-20 23:59 UTC: BTC taker 0.07 -> 0.05
def load(tag):
    rows = []
    for f in sorted(D.glob(f"{tag}_*.pkl")):
        rows += pickle.load(open(f, "rb"))
    rows = [r for r in rows if r["out"] in "WL" and r["out"]]
    for r in rows:
        win = 1.0 if r["out"] == "W" else 0.0
        fr = (0.07 if r["x"] < FEECUT else 0.05) if r["role"] == "taker" else 0.0
        r["pnl2"] = ((win - r["p"]) if r["side"] == "BUY" else (r["p"] - win)) * r["sz"] - fr * r["p"] * (1 - r["p"]) * r["sz"]
    return rows
def stat(rows, B=2000):
    if not rows:
        return None
    pnl = np.array([r["pnl2"] for r in rows]); usd = np.array([r["usd"] for r in rows])
    uk, inv = np.unique([r["key"] for r in rows], return_inverse=True)
    mp, mu = np.bincount(inv, pnl), np.bincount(inv, usd)
    pick = np.random.default_rng(0).integers(0, len(uk), (B, len(uk)))
    lo, hi = np.percentile(100 * mp[pick].sum(1) / mu[pick].sum(1), [5, 95])
    buys = [r for r in rows if r["side"] == "BUY"]
    return dict(n=len(rows), mk=len(uk), usd=usd.sum(), pnl=pnl.sum(), c=100 * pnl.sum() / usd.sum(), lo=lo, hi=hi,
                se=(hi - lo) / 3.29, win=np.mean([r["out"] == "W" for r in buys]) if buys else float("nan"),
                maker=np.mean([r["role"] == "maker" for r in rows]), buy=len(buys) / len(rows))
def line(name, s):
    if s is None:
        return f"| {name} | 0 | | | | | | | |"
    return (f"| {name} | {s['n']:,} | {s['mk']} | {s['usd']:,.0f} | {s['pnl']:+,.0f} | {s['c']:+.2f} [{s['lo']:+.2f}, {s['hi']:+.2f}] | "
            f"{s['win']:.3f} | {s['maker']:.0%} | {s['buy']:.0%} |")
H = "| set | fills | markets | $ | P&L $ | ¢/$ [90% CI by market] | win rate (buys) | maker share | buy share |\n|---|---|---|---|---|---|---|---|---|"
out = []
for tag, name in (("cca3d4_5", "0xcca3d4 BTC 5m"), ("b1d304_5", "0xb1d304 BTC 5m"), ("13c621_5", "0x13c621 BTC 5m"), ("239e726f_15", "0x239e726f BTC 15m")):
    rows = load(tag)
    out.append(f"\n#### {name}\n\n{H}")
    pre = [r for r in rows if r["x"] < SPLIT]; post = [r for r in rows if r["x"] >= SPLIT]
    out.append(line("all 11-14..23", stat(rows)))
    out.append(line("before 11-16 13:00 (selection)", stat(pre)))
    out.append(line("**after 11-16 13:00 (out of sample)**", stat(post)))
    for role in ("maker", "taker"):
        out.append(line(f"after, {role} only", stat([r for r in post if r["role"] == role])))
    days = {}
    for r in rows:
        days.setdefault(time.strftime("%m-%d", time.gmtime(r["x"] / 1000)), []).append(r)
    out.append("\nBy day (¢/$ [90% CI by market], $ P&L, fills): " + "; ".join(
        f"{d} {s['c']:+.1f} [{s['lo']:+.1f}, {s['hi']:+.1f}] {s['pnl']:+,.0f}$ n={s['n']}" for d, s in ((d, stat(v)) for d, v in sorted(days.items()))))
    if tag == "cca3d4_5":
        def cut(title, key, edges):
            out.append(f"\n{name}, {title}: before vs after the split\n\n| bucket | before n | before ¢/$ [CI] | after n | after ¢/$ [CI] | after share of $ |\n|---|---|---|---|---|---|")
            tot = sum(r["usd"] for r in post)
            for lo, hi in zip(edges[:-1], edges[1:]):
                a = [r for r in pre if r.get(key) is not None and lo <= r[key] < hi]
                b = [r for r in post if r.get(key) is not None and lo <= r[key] < hi]
                sa, sb = stat(a), stat(b)
                f = lambda s: f"{s['c']:+.2f} [{s['lo']:+.2f}, {s['hi']:+.2f}]" if s and s["mk"] >= 5 else (f"{s['c']:+.2f} (<5 markets)" if s else "")
                out.append(f"| {lo:g} to {hi:g} | {len(a)} | {f(sa)} | {len(b)} | {f(sb)} | {sum(r['usd'] for r in b) / tot:.0%} |")
        for r in rows:
            r["vsmid_c"] = 100 * r["vs_mid"] if r["vs_mid"] == r["vs_mid"] else None
            r["own1s_abs"] = abs(r["own1s"]) if r.get("own1s") is not None and r["own1s"] == r["own1s"] else None
            r["buy_maker"] = (r["side"] == "BUY") * 2 + (r["role"] == "maker")
        cut("token price", "p", (0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.01))
        cut("seconds left", "left", (0, 10, 30, 60, 120, 180, 301))
        cut("fill price − token mid (¢)", "vsmid_c", (-99, -2, -1, -0.5, 0, 0.5, 1, 2, 99))
        cut("|own mid move over the previous 1 s| (¢)", "own1s_abs", (0, 0.25, 1.5, 99))
        cut("side×role (0 sell-taker, 1 sell-maker, 2 buy-taker, 3 buy-maker)", "buy_maker", (0, 1, 2, 3, 4))
print("\n".join(out))
