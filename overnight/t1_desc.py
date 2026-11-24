"""T1 descriptive rows from per-day files: pass check, delay grid, by day, fee regime, races, maker 712772, quote persistence."""
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

mode, since, until = sys.argv[1], sys.argv[2], sys.argv[3]
OUT = Path(__file__).resolve().parent / "out" / "t1" / mode
rows = []
for p in sorted(OUT.glob("*.pkl")):
    if since <= p.stem <= until:
        rows += [dict(w, day=p.stem) for w in pickle.load(p.open("rb"))]
print(f"{mode} {since}..{until}: {len(rows)} windows over {len({w['day'] for w in rows})} days")
rng = np.random.default_rng(0)


def ci(per_w, days):
    x = np.asarray(per_w, float)
    t = x[rng.integers(0, len(x), (4000, len(x)))].sum(1) / days
    return np.percentile(t, 5), np.percentile(t, 95), x.sum() / days, x.std() * np.sqrt(len(x)) / days


def cell(ws, d1, d2, filt=lambda f: True):
    per_w, sh, n = [], 0.0, 0
    for w in ws:
        s = 0.0
        for f in w["fills"]:
            if (f["d1"], f["d2"]) == (d1, d2) and f["hedged"] and filt(f):
                s += f["pnl"] * f["qty"]
                sh += f["qty"]
                n += 1
        per_w.append(s)
    days = len(ws) / 96
    lo, hi, mean, se = ci(per_w, days)
    return dict(n=n, cps=100 * sum(per_w) / max(sh, 1e-9), usd=mean, lo=lo, hi=hi, se=se, perday=n / days, sh=sh / days)


print("\nasset | delay (Lighter+hedge ms) | fills | fills/day | c/share | $/day [90%] | SE $/day | min detectable (2.5 SE)")
for nm in ("btc", "eth"):
    ws = [w for w in rows if w["asset"] == nm]
    for d1, d2 in ((500, 500), (250, 250), (300, 500), (140, 500), (500, 1000), (1000, 500), (1000, 1000), (2000, 2000)):
        c = cell(ws, d1, d2)
        print(f"{nm} | {d1}+{d2} | {c['n']} | {c['perday']:.1f} | {c['cps']:+.2f} | {c['usd']:+.1f} [{c['lo']:+.1f}, {c['hi']:+.1f}] | {c['se']:.1f} | {2.5 * c['se']:.1f}")

print("\nby day, 500+500: day | BTC fills, c/sh, $ | ETH fills, c/sh, $ | PM fee BTC")
for day in sorted({w["day"] for w in rows}):
    out = []
    for nm in ("btc", "eth"):
        ws = [w for w in rows if w["asset"] == nm and w["day"] == day]
        fs = [f for w in ws for f in w["fills"] if (f["d1"], f["d2"]) == (500, 500) and f["hedged"]]
        usd = sum(f["pnl"] * f["qty"] for f in fs)
        out.append(f"{len(fs)}, {100 * usd / max(sum(f['qty'] for f in fs), 1e-9):+.2f}, {usd:+.0f}")
    fees = sorted({w["fee"] for w in rows if w["asset"] == "btc" and w["day"] == day})
    print(f"{day} | {out[0]} | {out[1]} | {fees}")

print("\nfee regime (BTC PM fee per window), 500+500:")
for nm in ("btc", "eth"):
    for lab, f_ in (("pre (0.01)", lambda w: w["fee"] < 0.03), ("post (0.05)", lambda w: w["fee"] >= 0.03)):
        ws = [w for w in rows if w["asset"] == nm and (f_(w) if nm == "btc" else (w["e"] < 1795191300 if "pre" in lab else w["e"] >= 1795191300))]
        if ws:
            c = cell(ws, 500, 500)
            print(f"  {nm} {lab}: {len(ws)} windows, fills {c['n']}, {c['cps']:+.2f}c/share, ${c['usd']:+.1f}/day [{c['lo']:+.1f}, {c['hi']:+.1f}]")

print("\n500+500 fill attributes: late (last 2 min), small quote (<=30 sh), raced (real Lighter taker same side within 1 s), displayed at +300/+500 ms, maker 712772 (proxy)")
for nm in ("btc", "eth"):
    fs = [f for w in rows if w["asset"] == nm for f in w["fills"] if (f["d1"], f["d2"]) == (500, 500) and f["hedged"]]
    usd = sum(f["pnl"] * f["qty"] for f in fs)
    late = sum(f["pnl"] * f["qty"] for f in fs if f["t_rel"] >= 780)
    small = sum(f["pnl"] * f["qty"] for f in fs if f["small"])
    mk = [f["maker712772"] for f in fs if "maker712772" in f]
    rc = [f for f in fs if f.get("raced")]
    nrc = [f for f in fs if not f.get("raced")]
    cs = lambda L: 100 * sum(f["pnl"] * f["qty"] for f in L) / max(sum(f["qty"] for f in L), 1e-9)  # noqa: E731
    print(f"  {nm}: fills {len(fs)}; $ from last 2 min {late / usd:.2f}; $ from small quotes {small / usd:.2f}; raced {len(rc) / len(fs):.2f} "
          f"(raced {cs(rc):+.2f}c vs not {cs(nrc):+.2f}c); still displayed +300 ms {np.mean([f['disp300'] for f in fs]):.3f}, +500 ms {np.mean([f['disp500'] for f in fs]):.3f}; "
          f"nearest same-side print by 712772 {np.mean(mk):.2f} (of {len(mk)} fills with a print within 60 s); hedge pnl<0 share {np.mean([f['pnl'] < 0 for f in fs]):.2f}")
    tot = [w["maker_sh"] for w in rows if w["asset"] == nm]
    print(f"     712772 share of all Lighter maker volume: {sum(a for a, b in tot) / max(sum(b for a, b in tot), 1e-9):.2f}")
    trig = sum(w["trig"].get((500, 500), 0) for w in rows if w["asset"] == nm)
    print(f"     triggers {trig}, Lighter leg filled {len([f for w in rows if w['asset'] == nm for f in w['fills'] if (f['d1'], f['d2']) == (500, 500)])}, of which hedged {len(fs)}")
