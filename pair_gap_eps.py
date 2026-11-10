"""Drill into 'executable' taker-arb episodes from pair_gap_scan: real or a lagging token feed?

Polymarket matches complementary orders (mint/merge), so the true book is mirrored and ask_U+ask_D>=1.
A sum<1 needs one token's stream to be stale. Tell-tale: the leg whose quote is older at onset is the
leg whose update ends the episode, and its displayed ask is the 'cheap' (mirror-violating) one.
"""

import argparse
import csv
from pathlib import Path

import numpy as np

from same_token_wf2 import LAT_US, SIZE, day_of

ap = argparse.ArgumentParser()
ap.add_argument("--cache", type=Path, required=True)
ap.add_argument("--out", type=Path, required=True)
ap.add_argument("--all", action="store_true", help="all net-of-fee episodes, not only executable")
a = ap.parse_args()

E = np.concatenate([np.load(f)["eps"] for f in sorted(a.out.glob("????-??-??.npz"))])
sel = (E[:, 4] > 0) if a.all else (np.nan_to_num(E[:, 5], nan=-1) > 0)
E = E[sel]
rows = []
for ep in E:
    e0, t0 = int(ep[0]), int(ep[1])
    m = np.load(a.cache / day_of(e0) / f"{e0}.npz")
    t, bid, ask, asz, upd, seen = (m[k] for k in ("t", "bid", "ask", "asz", "upd", "seen"))
    bid, ask = bid.astype(int), ask.astype(int)
    s = int(np.searchsorted(t, t0))
    x = int(np.searchsorted(t, t0 + LAT_US, "right")) - 1
    e = s
    while e < len(t) and bid[e].min() >= 0 and ask[e].sum() < 1000:
        e += 1
    age_s = t[s] - seen[s]
    age_x = t[x] - seen[x]
    mir = np.array([ask[x, 0] - (1000 - bid[x, 1]), ask[x, 1] - (1000 - bid[x, 0])])
    cheap = int(np.argmin(mir))  # leg priced below its mirror
    older = int(np.argmax(age_x))
    ender = -1 if e >= len(t) else (0 if upd[e, 0] and not upd[e, 1] else 1 if upd[e, 1] and not upd[e, 0] else 2)
    rows.append(
        dict(
            day=day_of(e0),
            epoch=e0,
            t_mkt_s=round(t0 / 1e6, 2),
            dur_ms=round(ep[2] / 1000, 1),
            raw_edge=int(ep[3]),
            net_exec=round(ep[5], 2),
            pnl_usd=round(ep[5] * SIZE / 1000, 3),
            bidU=bid[x, 0],
            askU=ask[x, 0],
            bidD=bid[x, 1],
            askD=ask[x, 1],
            ageU_ms=round(age_x[0] / 1000, 1),
            ageD_ms=round(age_x[1] / 1000, 1),
            onset_age_old_ms=round(age_s.max() / 1000, 1),
            mirU=mir[0],
            mirD=mir[1],
            cheap="UD"[cheap],
            older="UD"[older],
            ended_by="-UDB"[ender + 1],
            aszU=float(asz[x, 0]),
            aszD=float(asz[x, 1]),
        )
    )

p = a.out / ("episodes_net.csv" if a.all else "episodes_exec.csv")
with p.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)

r = rows
n = len(r)
oa = np.array([max(x["ageU_ms"], x["ageD_ms"]) for x in r])
print(f"{n} episodes -> {p}")
print(f"pnl ${sum(x['pnl_usd'] for x in r):.2f}; days {sorted({x['day'] for x in r})[:3]}..")
print(f"older-leg quote age at +lat ms p10/50/90: {np.percentile(oa, [10, 50, 90]).round(0)}")
print(f"older leg is the cheap leg: {np.mean([x['cheap'] == x['older'] for x in r]):.2f}")
print(f"episode ended by update of the older leg: {np.mean([x['ended_by'] == x['older'] for x in r]):.2f}")
print(f"older age >= 250 ms: {np.mean(oa >= 250):.2f}; >= 1000 ms: {np.mean(oa >= 1000):.2f}")
print(f"time in market s p10/50/90: {np.percentile([x['t_mkt_s'] for x in r], [10, 50, 90]).round(0)}")
print(f"dur ms p10/50/90: {np.percentile([x['dur_ms'] for x in r], [10, 50, 90]).round(0)}")
print(f"net edge milli p10/50/90: {np.percentile([x['net_exec'] for x in r], [10, 50, 90]).round(1)}")
print(f"min leg size at exec p10/50: {np.percentile([min(x['aszU'], x['aszD']) for x in r], [10, 50]).round(0)}")
print("top 10 by pnl:")
for x in sorted(r, key=lambda x: -x["pnl_usd"])[:10]:
    print(" ", {k: getattr(x[k], "item", lambda: x[k])() for k in ("day", "epoch", "t_mkt_s", "dur_ms", "net_exec", "bidU", "askU", "bidD", "askD",
                                  "ageU_ms", "ageD_ms", "cheap", "ended_by")})
