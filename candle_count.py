#!/usr/bin/env python3
"""Candle count: do the outcomes of earlier BTC 5m up/down windows predict the next one, and is it already in the
opening price?  Polymarket data only (Gamma winners; optional June-July book cache from same_token_wf2 prep).

  candle_count.py fetch --start 2026-06-01 --end 2026-11-17 --out ~/cc-outcomes.csv [--seed ~/stwf2-cache/settlements.csv]
  candle_count.py eval  --outcomes ~/cc-outcomes.csv [--split 2026-08-01] [--cache ~/stwf2-cache]

A 1c/share edge is a 1-point shift in win probability, so everything is reported in points with its standard error.
"""
import argparse, csv, json, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

NS = (1, 2, 3, 5, 10, 20, 50, 100, 288)   # rolling lengths, in 5m windows (288 = one day)
GAMMA = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&"


def ep(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def read(p):
    return {int(r["slug"].rsplit("-", 1)[1]): r["winner"] for r in csv.DictReader(Path(p).open())} if Path(p).exists() else {}


def cmd_fetch(a):
    have = {}
    for p in (a.seed or []) + [a.out]:
        have.update(read(p))
    todo = [e for e in range(ep(a.start), ep(a.end) + 86400, 300) if e not in have and e + 900 < time.time()]
    print(f"have {len(have)}, to fetch {len(todo)}", flush=True)

    def get(batch):
        url = GAMMA + "&".join(f"slug=btc-updown-5m-{e}" for e in batch)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
            return json.load(r)

    def save():
        with Path(a.out).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "winner"])
            w.writerows((f"btc-updown-5m-{e}", v) for e, v in sorted(have.items()))

    for i in range(0, len(todo), 50):
        try:
            for m in get(todo[i:i + 50]):
                names, px = [n.upper() for n in json.loads(m["outcomes"])], [float(v) for v in json.loads(m["outcomePrices"])]
                if m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(px) >= 0.99 and min(px) <= 0.01:
                    have[int(m["slug"].rsplit("-", 1)[1])] = names[px.index(max(px))]
        except Exception as e:  # noqa: BLE001
            print("fetch failed", i, repr(e)[:120], flush=True)
        if i % 5000 == 0:
            save()
            print(f"{i}/{len(todo)} have {len(have)}", flush=True)
        time.sleep(a.sleep)
    save()
    print(f"wrote {a.out}: {len(have)} winners")


# ---------------------------------------------------------------- pure (unit-tested)
def rolling(E, U, n):
    """-> (idx, f): windows whose n predecessors are all present, and the UP share of those n."""
    c = np.r_[0, np.cumsum(U)]
    i = np.arange(n, len(E))
    i = i[E[i] - E[i - n] == 300 * n]
    return i, (c[i] - c[i - n]) / n


def pts(k, n):
    """Win rate minus 50%, in points, and its standard error."""
    return (100 * (k / n - 0.5), 50 / np.sqrt(n)) if n else (float("nan"), float("nan"))


def bets(E, U, n, d):
    """d=+1: bet with the majority of the last n; d=-1: against. -> (idx, win 0/1); ties place no bet."""
    i, f = rolling(E, U, n)
    s = np.sign(f - 0.5) * d
    k = s != 0
    return i[k], (U[i[k]] == (s[k] > 0)).astype(int)


def streaks(E, U, cap=7):
    """-> {L: (n, continued)}: after exactly L equal outcomes in a row (L capped), how often the next was the same."""
    out, run = {}, 1
    for i in range(1, len(U)):
        if E[i] - E[i - 1] != 300:
            run = 0   # unknown history: the run restarts with this window
        elif run:
            L = min(run, cap)
            n, k = out.get(L, (0, 0))
            out[L] = (n + 1, k + int(U[i] == U[i - 1]))
        run = run + 1 if run and E[i] - E[i - 1] == 300 and U[i] == U[i - 1] else 1
    return out


def walk_forward(E, U, look=30, cells=None):
    """Each day: pick the (n, d) cell with the most net wins over the previous `look` days, bet it that day.
    -> (day index per bet, win 0/1 per bet, picks)."""
    cells = cells or [(n, d) for n in NS for d in (1, -1)]
    day = E // 86400
    B = []
    for n, d in cells:
        i, w = bets(E, U, n, d)
        B.append((day[i], w))
    D, W, picks = [], [], []
    for t in np.unique(day):
        sc = [(2 * w[(dd >= t - look) & (dd < t)].sum() - ((dd >= t - look) & (dd < t)).sum()) for dd, w in B]
        if t - day.min() < look or max(sc) <= 0:
            continue
        b = int(np.argmax(sc))
        k = B[b][0] == t
        D.append(B[b][0][k]), W.append(B[b][1][k]), picks.append(cells[b])
    return (np.concatenate(D), np.concatenate(W), picks) if D else (np.array([]), np.array([]), [])


def day_ci(day, w, n=2000, seed=1):
    """90% interval of (win rate - 50%) in points, resampling days."""
    u, inv = np.unique(day, return_inverse=True)
    k, m = np.bincount(inv, w), np.bincount(inv)
    r = np.random.default_rng(seed).integers(0, len(u), (n, len(u)))
    v = 100 * (k[r].sum(1) / m[r].sum(1) - 0.5)
    return round(float(np.percentile(v, 5)), 2), round(float(np.percentile(v, 95)), 2)


def quote_at(m, s_us, age=2_000_000, gap=10_000_000):
    """UP bid/ask (dollars) standing s_us after the open. None if missing, crossed, the UP side was stale at the
    book's last event (as in same_token_wf2), or the book had no event at all for `gap` before s_us."""
    j = int(np.searchsorted(m["t"], s_us, "right")) - 1
    if (j < 0 or m["bid"][j, 0] < 0 or m["ask"][j, 0] <= m["bid"][j, 0] or m["t"][j] - m["seen"][j, 0] > age
            or s_us - m["t"][j] > gap):
        return None
    return m["bid"][j, 0] / 1000, m["ask"][j, 0] / 1000


# ---------------------------------------------------------------- report
def cmd_eval(a):
    O = read(a.outcomes)
    E = np.array(sorted(O))
    U = np.array([O[e] == "UP" for e in E]).astype(int)
    split = ep(a.split)
    parts = [("all", E > 0), (f"before {a.split}", E < split), (f"from {a.split}", E >= split)]
    print(f"markets {len(E)}, {datetime.fromtimestamp(E[0], timezone.utc):%F} .. {datetime.fromtimestamp(E[-1], timezone.utc):%F}, "
          f"UP won {U.mean():.4f}. 1 point = 1c/share at a fair fill. Cells looked at below: {2 * len(NS) + 7}")

    print("\nA. Bet AGAINST the majority of the last N windows (negative = betting WITH it wins). points +- SE, z")
    print("   N    " + "".join(f"{lab:>28s}" for lab, _ in parts))
    for n in NS:
        i, w = bets(E, U, n, -1)
        row = f"   {n:<4d} "
        for _, msk in parts:
            k = msk[i]
            p, se = pts(w[k].sum(), k.sum())
            row += f"{p:+7.2f} +-{se:4.2f} z{p / se:+5.1f} n{k.sum():6d}"
        print(row)

    print("\nB. After L equal outcomes in a row: how often the next one is the same, in points vs 50% +- SE (n)."
          "\n   Negative = the streak tends to break. L2 is 'two the same, what about the third'.")
    for lab, msk in parts:
        S = streaks(E[msk], U[msk])
        print(f"   {lab:18s} " + " | ".join(f"L{L}{'+' if L == 7 else ''} {pts(k, n)[0]:+.2f}+-{pts(k, n)[1]:.2f} ({n})" for L, (n, k) in sorted(S.items())))

    print("\nC. By month, betting against the previous window (N=1): points, n")
    i, w = bets(E, U, 1, -1)
    mon = np.array([datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m") for e in E[i]])
    print("   " + " | ".join(f"{m_} {pts(w[mon == m_].sum(), (mon == m_).sum())[0]:+.2f} ({(mon == m_).sum()})" for m_ in sorted(set(mon))))
    day = E[i] // 86400
    print(f"   all: {pts(w.sum(), len(w))[0]:+.2f}, 90% by days {day_ci(day, w)}; from {a.split}: "
          f"{pts(w[E[i] >= split].sum(), (E[i] >= split).sum())[0]:+.2f}, 90% {day_ci(day[E[i] >= split], w[E[i] >= split])}")

    print("\nD. Walk-forward: each day bet the (N, with/against) cell that won most over the previous 30 days")
    D, W, picks = walk_forward(E, U)
    if len(W):
        import collections
        print(f"   bets {len(W)}, points {pts(W.sum(), len(W))[0]:+.2f} +-{pts(W.sum(), len(W))[1]:.2f}, 90% by days {day_ci(D, W)}; "
              f"most picked {collections.Counter(picks).most_common(4)}")

    print("\nG. Dose: bet against the majority of the last N, by how lopsided the count was (majority's share)."
          f"\n   points +- SE (n): all | from {a.split}. A real effect should grow with the share.")
    for n in (3, 5, 10, 20, 50):
        i, f = rolling(E, U, n)
        lop, win = np.abs(f - 0.5) * 2, (U[i] == (f < 0.5)).astype(int)
        row = f"   N={n:<3d}"
        for lo, hi, lab in ((0, .25, "51-62%"), (.25, .5, "63-75%"), (.5, .75, "76-87%"), (.75, 1, "88-100%")):
            k = (lop > lo) & (lop <= hi)
            k2 = k & (E[i] >= split)
            if k.sum() >= 200:
                row += f" | {lab} {pts(win[k].sum(), k.sum())[0]:+.2f}+-{pts(0, k.sum())[1]:.2f} ({k.sum()}) | {pts(win[k2].sum(), k2.sum())[0]:+.2f}+-{pts(0, max(1, k2.sum()))[1]:.2f}"
        print(row)

    if a.cache:
        from same_token_wf2 import done_days, markets

        def side_map(n, lo=0.0):
            """epoch -> True if the bet is UP: against the majority of the last n, where lopsidedness > lo."""
            i, f = rolling(E, U, n)
            k = np.abs(f - 0.5) * 2 > lo
            return dict(zip(E[i[k]].tolist(), (f[k] < 0.5).tolist()))

        SIG = {"against the previous window": side_map(1), "against majority of last 3": side_map(3),
               "against majority of last 5": side_map(5), "  last 5 was 4-1 or 5-0": side_map(5, 0.5),
               "against majority of last 20": side_map(20), "  last 20 was 13-7 or more": side_map(20, 0.25),
               "against majority of last 50": side_map(50)}
        fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731
        R, RS = {(g, s): [] for g in SIG for s in (5, 30)}, []
        for ddir in done_days(Path(a.cache)):
            for e, m in markets(ddir):
                if e not in O:
                    continue
                up = O[e] == "UP"
                for s in (5, 30):
                    q = quote_at(m, s * 1_000_000)
                    if not q:
                        continue
                    for g, mp in SIG.items():
                        if e in mp:
                            b, k = q if mp[e] else (1 - q[1], 1 - q[0])   # the chosen side's bid, ask
                            R[(g, s)].append((b, k, int(up == mp[e])))
                    if s == 5 and e - 300 in O:
                        L = 1   # length of the streak the previous window ended, capped at 5
                        while L < 5 and O.get(e - 300 * (L + 1)) == O[e - 300]:
                            L += 1
                        pu = O[e - 300] == "UP"
                        b, k = (1 - q[1], 1 - q[0]) if pu else q
                        RS.append((b, k, int(up != pu), L))

        def line(lab, rows):
            x = np.array(rows, float)
            mid, win = (x[:, 0] + x[:, 1]) / 2, x[:, 2]
            return (f"   {lab:30s} n {len(x):6d}   its mid {mid.mean():.4f} +-{mid.std() / np.sqrt(len(x)):.4f}   wins {win.mean():.4f}   "
                    f"edge at mid {100 * (win - mid).mean():+.2f} +-{100 * (win - mid).std() / np.sqrt(len(x)):.2f}   "
                    f"at its bid {100 * (win - x[:, 0]).mean():+.2f}   at its ask+fee {100 * (win - x[:, 1] - fee(x[:, 1])).mean():+.2f}")

        print("\nE. Is it priced? The side each rule would buy, as quoted after the open (book cache). 'its mid' above 0.5000"
              "\n   means the book already leans that way; 'edge at mid' is what is left, in points.")
        for s in (5, 30):
            print(f"   -- {s} s after the open")
            for g in SIG:
                if R[(g, s)]:
                    print(line(g, R[(g, s)]))
        print("\nF. The side AGAINST the streak, 5 s after the open, by the length of the streak (5 = five or more)")
        for L in (1, 2, 3, 4, 5):
            rows = [r[:3] for r in RS if r[3] == L]
            if rows:
                print(line(f"streak {L}{'+' if L == 5 else ''}", rows))
        rows = [r[:3] for r in RS if r[3] >= 3]
        if rows:
            print(line("streak 3 or more", rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch")
    f.add_argument("--start", required=True), f.add_argument("--end", required=True)
    f.add_argument("--out", type=Path, required=True), f.add_argument("--seed", type=Path, action="append")
    f.add_argument("--sleep", type=float, default=0.15)
    f.set_defaults(fn=cmd_fetch)
    e = sp.add_parser("eval")
    e.add_argument("--outcomes", type=Path, required=True), e.add_argument("--split", default="2026-08-01")
    e.add_argument("--cache", type=Path)
    e.set_defaults(fn=cmd_eval)
    a = ap.parse_args()
    a.fn(a)
