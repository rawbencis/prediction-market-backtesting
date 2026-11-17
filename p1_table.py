"""Part 1 tables from out/p1/p1-{day}.pkl (dev days) -> markdown on stdout."""
import pickle
import sys
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "out" / "p1"
DAYS = sys.argv[1:] or ["2026-11-14", "2026-11-15", "2026-11-16"]
A = {}
for d in DAYS:
    for k, v in pickle.load(open(OUT / f"p1-{d}.pkl", "rb")).items():
        if k[0] in ("prints_n", "minutes", "lead_nomatch"):
            A[k] = [A.get(k, [0])[0] + v[0]]
        else:
            A.setdefault(k, []).extend(v)
LB = {15: ["14-15", "10-14", "5-10", "3-5", "1-3", "0-1"], 5: ["3-5", "1-3", "0-1"]}  # minutes left, labels by bucket index reversed below
EDGES = {15: [0, 1, 3, 5, 10, 15], 5: [0, 1, 3, 5]}


def q(v, p=(10, 50, 90), fmt="{:.0f}"):
    v = np.asarray(v, float)
    v = v[~np.isnan(v)]
    return "/".join(fmt.format(x) for x in np.percentile(v, p)) if len(v) else "-"


def share(v, cond):
    v = np.asarray(v, float)
    return np.mean(cond(v)) if len(v) else np.nan


print("### Book anatomy by minutes left (1-s samples; prints per minute per window; dev days " + ", ".join(DAYS) + ")\n")
print("| tf | minutes left | spread ¢ p10/50/90 | share <= 1¢ | min(best sizes) p10/50/90 sh | prints/min | print size p50/p90 sh |")
print("|---|---|---|---|---|---|---|")
for tf in (5, 15):
    E = EDGES[tf]
    for lb in range(len(E) - 1):
        sp = A.get(("spread", tf, lb), [])
        mins = A.get(("minutes", tf, lb), [0])[0]
        n = A.get(("prints_n", tf, lb), [0])[0]
        ps = A.get(("print_sz", tf, lb), [])
        print(f"| {tf}m | {E[lb]}-{E[lb + 1]} | {q(sp, fmt="{:.1f}")} | {share(sp, lambda v: v <= 1.0001):.2f} | {q(A.get(('bestsize', tf, lb), []))} | "
              f"{n / max(mins, 1e-9):.1f} | {q(ps, (50, 90))} |")
print("\n### Speed of the book (ms; p10/p50/p90; share resolved within the window)\n")
print("| measure | BTC 5m | BTC 15m |")
print("|---|---|---|")
for key, lab in (("lvl_life", "new best level: lifetime"), ("past1", "best bid 1 tick past a newly joined level"),
                 ("past2", "best bid 2 ticks past"), ("imb_decay", "own imbalance >= 0.6 -> < 0")):
    cells = []
    for tf in (5, 15):
        v = np.asarray(A.get((key, tf), []), float)
        cells.append(f"{q(v)} ({np.mean(~np.isnan(v)):.0%} happen; n {len(v):,})" if len(v) else "-")
    print(f"| {lab} | {cells[0]} | {cells[1]} |")
for tf in (5, 15):
    pass
print("\n### Load and receive delay\n")
print("| | BTC 5m | BTC 15m |")
print("|---|---|---|")
r5, r15 = A.get(("rows_per_s", 5), []), A.get(("rows_per_s", 15), [])
print(f"| book rows/s (seconds with rows) p50/p99/max | {q(r5, (50, 99, 100))} | {q(r15, (50, 99, 100))} |")
print(f"| receive delay l - x ms, all p50/p90/p99 | {q(A.get(('rx_delay', 5), []), (50, 90, 99), '{:.1f}')} | {q(A.get(('rx_delay', 15), []), (50, 90, 99), '{:.1f}')} |")
print(f"| receive delay in the busiest 1% seconds p50/p90/p99 | {q(A.get(('rx_delay_busy', 5), []), (50, 90, 99), '{:.1f}')} | {q(A.get(('rx_delay_busy', 15), []), (50, 90, 99), '{:.1f}')} |")
print("\n### Pull signals for a resting BTC 15m UP bid (per window minute; survival = no signal for the hold time, from random times)\n")
print("| signal | THR | alts | raw triggers/min | episodes/min (>1 s apart) | survive 15 s | 60 s | 180 s |")
print("|---|---|---|---|---|---|---|---|")
names = sorted({k[1] for k in A if k[0] == "sig_rate"}, key=lambda s: (s.split("|")[0], int(s.split("|")[1]), int(s.split("|")[2])))
for nm in names:
    src, thr, need = nm.split("|")
    sv = [np.mean(A.get(("sig_surv", nm, h), [np.nan])) for h in (15000, 60000, 180000)]
    print(f"| {src} | {int(thr) / 100:.1f}c | {need} | {np.mean(A[('sig_rate', nm)]):.0f} | {np.mean(A.get(('sig_episodes', nm), [np.nan])):.1f} | "
          f"{sv[0]:.2f} | {sv[1]:.2f} | {sv[2]:.2f} |")
print("\n### Which book moves first (episode starts: mid move >= 0.5c in 500 ms after 1 s quiet; nearest same-direction start of the other book within +-1 s)\n")
print("| A -> B | matched | share A first | lag B - A ms p25/p50/p75 | share lag > 190 ms |")
print("|---|---|---|---|---|")
for k in sorted(k for k in A if k[0] == "lead"):
    v = np.asarray(A[k], float)
    nm = A.get(("lead_nomatch", k[1], k[2]), [0])[0]
    print(f"| {k[1]} -> {k[2]} | {len(v):,} of {len(v) + nm:,} | {np.mean(v > 0):.2f} | {q(v, (25, 50, 75))} | {np.mean(v > 190):.2f} |")
