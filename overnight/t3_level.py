"""T3: level map on June-July BTC 5m (shared grid with Dublin; definitions kept exactly).

  extract   one row per (window, 5 s sample, side) -> out/t3/samples.npz   (streams ~/stwf2-cache one window at a time)
  dev       84-cell table on dev, cell W by r, candidate cells -> stdout
  holdout   frozen cells (frozen/t3_cells.json) on holdout; log-loss test; favourites by 6-hour block

Unit: one settled BTC 5m window. Samples every 5 s from open+10 s to close-10 s. Side s in {UP, DOWN}.
d = 1e4 ln(S/K) signed toward s; S = Binance 1 s close (last completed kline), K = S at the open second;
sigma_l = sd of 1 s log returns over previous 30 min, sigma_s = over previous 60 s; tau = seconds left;
z = d / (sigma_l sqrt(tau)); r = sigma_s / sigma_l. Trade: taker BUY of s at the ask standing 250 ms after
the sample, needs >= 5 shares shown; one per window per cell (first sample in the cell). Fee 0.05 p(1-p).
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from math import erf, sqrt
from pathlib import Path

import numpy as np

CACHE = Path("/home/ubuntu/stwf2-cache")
BTC = Path("/home/ubuntu/btc1s")
OUT = Path(__file__).resolve().parent / "out" / "t3"
ZB = np.array([-np.inf, -2, -1, -0.3, 0.3, 1, 2, np.inf])
ZL = ("<=-2", "(-2,-1]", "(-1,-0.3]", "(-0.3,0.3]", "(0.3,1]", "(1,2]", ">2")
TB = np.array([10, 60, 120, 180, 290.0001])
TL = ("[10,60)", "[60,120)", "[120,180)", "[180,290]")
RL = ("<0.7", "[0.7,1.4]", ">1.4")
FEE = 0.05
HOLD_START, HOLD_END = "2026-07-19", "2026-07-31"
DEV_EXCL = ("2026-06-01", "2026-06-02", "2026-06-03")


def zbin(z):
    return np.searchsorted(ZB, z, "left") - 1  # (lo, hi] bins


def tbin(t):
    return np.searchsorted(TB, t, "right") - 1


def rbin(r):
    return np.where(r < 0.7, 0, np.where(r <= 1.4, 1, 2))


class Btc:
    def __init__(self):
        parts = [np.load(f) for f in sorted(BTC.glob("BTCUSDT-1s-*.npz"))]
        sec = np.concatenate([p["sec"] for p in parts])
        close = np.concatenate([p["close"] for p in parts])
        o = np.argsort(sec)
        self.s0 = int(sec[o][0])
        n = int(sec[o][-1]) - self.s0 + 1
        px = np.full(n, np.nan)
        px[sec[o] - self.s0] = close[o]
        ok = ~np.isnan(px)
        idx = np.where(ok, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        self.px = px[idx]
        self.lr = np.r_[0.0, np.diff(np.log(self.px))]
        c = np.r_[0.0, np.cumsum(self.lr)]
        c2 = np.r_[0.0, np.cumsum(self.lr ** 2)]
        self.c, self.c2 = c, c2

    def i_known(self, t_s):
        """index of the last 1 s kline completed by time t_s (opened <= t_s - 1); its close."""
        return int(np.floor(t_s)) - 1 - self.s0

    def sd(self, i, n):
        """sd of lr[i-n+1 .. i]"""
        s = self.c[i + 1] - self.c[i - n + 1]
        s2 = self.c2[i + 1] - self.c2[i - n + 1]
        v = max(s2 / n - (s / n) ** 2, 0.0)
        return sqrt(v * n / (n - 1))


def cmd_extract(a):
    OUT.mkdir(parents=True, exist_ok=True)
    win = {}
    with (CACHE / "settlements.csv").open() as f:
        for r in csv.DictReader(f):
            win[int(r["slug"].rsplit("-", 1)[1])] = r["winner"]
    btc = Btc()
    cols = {k: [] for k in ("e", "s", "side", "d", "z", "tau", "r", "m1", "mid", "ask", "asksz", "win")}
    nwin = nskip = 0
    days = sorted(p for p in CACHE.iterdir() if p.is_dir())
    if a.days:
        days = [d for d in days if d.name in a.days]
    for day in days:
        for f in sorted(day.glob("[0-9]*.npz")):
            e = int(f.stem)
            if e not in win or win[e] not in ("UP", "DOWN"):
                nskip += 1
                continue
            i0 = btc.i_known(e)
            if i0 - 1800 < 0 or btc.i_known(e + 300) >= len(btc.px):
                nskip += 1
                continue
            m = np.load(f)
            t, bid, ask, asz = m["t"], m["bid"], m["ask"], m["asz"]
            if len(t) == 0:
                nskip += 1
                continue
            nwin += 1
            K = btc.px[i0]
            up_won = win[e] == "UP"
            for s in range(10, 295, 5):
                i = btc.i_known(e + s)
                S = btc.px[i]
                sl, ss = btc.sd(i, 1800), btc.sd(i, 60)
                if sl <= 0:
                    continue
                tau = 300 - s
                m1 = 1e4 * (np.log(S) - np.log(btc.px[i - 1]))
                k = np.searchsorted(t, s * 1_000_000, "right") - 1
                k2 = np.searchsorted(t, s * 1_000_000 + 250_000, "right") - 1
                if k < 0 or k2 < 0:
                    continue
                for side in (0, 1):
                    sg = 1.0 if side == 0 else -1.0
                    b_, a_ = bid[k, side], ask[k, side]
                    if b_ < 0 or a_ <= b_:
                        continue
                    a2 = ask[k2, side]
                    d = sg * 1e4 * np.log(S / K)
                    cols["e"].append(e)
                    cols["s"].append(s)
                    cols["side"].append(side)
                    cols["d"].append(d)
                    cols["z"].append(d / 1e4 / (sl * sqrt(tau)))
                    cols["tau"].append(tau)
                    cols["r"].append(ss / sl)
                    cols["m1"].append(sg * m1)
                    cols["mid"].append((b_ + a_) / 2000)
                    cols["ask"].append(a2 / 1000 if a2 > 0 else np.nan)
                    cols["asksz"].append(float(asz[k2, side]) if a2 > 0 else 0.0)
                    cols["win"].append(float(up_won if side == 0 else not up_won))
            del m
            if nwin % 1000 == 0:
                print(f"{day.name} windows {nwin}", flush=True)
    np.savez_compressed(OUT / (a.out or "samples.npz"), **{k: np.array(v) for k, v in cols.items()})
    print(f"windows {nwin}, skipped {nskip}, samples {len(cols['e'])}")


def load(split):
    X = dict(np.load(OUT / "samples.npz"))
    days = np.array([datetime.fromtimestamp(int(e), timezone.utc).strftime("%Y-%m-%d") for e in X["e"]])
    if split == "dev":
        m = (days < HOLD_START) & ~np.isin(days, DEV_EXCL)
    else:
        m = (days >= HOLD_START) & (days <= HOLD_END)
    X = {k: v[m] for k, v in X.items()}
    X["zb"], X["tb"], X["rb"] = zbin(X["z"]), tbin(X["tau"].astype(float)), rbin(X["r"])
    return X


def phi(x):
    return 0.5 * (1 + np.vectorize(erf)(np.asarray(x) / sqrt(2)))


def cell_stats(X, m):
    """one trade per window per cell: first sample (by s) per window, both sides counted separately."""
    idx = np.flatnonzero(m)
    if not len(idx):
        return None
    o = np.lexsort((X["s"][idx], X["side"][idx], X["e"][idx]))
    idx = idx[o]
    key = X["e"][idx] * 2 + X["side"][idx]
    _, first = np.unique(key, return_index=True)
    idx = idx[first]
    n = len(idx)
    fill = (X["asksz"][idx] >= 5) & np.isfinite(X["ask"][idx])
    w, mid = X["win"][idx], X["mid"][idx]
    em = w - mid
    f = idx[fill]
    a = X["ask"][f]
    en = X["win"][f] - a - FEE * a * (1 - a)
    # SE by window: a window can contribute both sides; cluster on e
    def se_cl(v, e):
        if len(v) < 3:
            return np.nan
        u, inv = np.unique(e, return_inverse=True)
        s = np.bincount(inv, v - v.mean())
        return sqrt((s ** 2).sum()) / len(v)
    return dict(n=n, windows=len(np.unique(X["e"][idx])), ask=float(np.nanmean(a)) if len(a) else np.nan, mid=float(mid.mean()),
                phi=float(phi(X["z"][idx]).mean()), win=float(w.mean()), em=float(em.mean()), em_se=se_cl(em, X["e"][idx]),
                en=float(en.mean()) if len(en) else np.nan, en_se=se_cl(en, X["e"][f]) if len(en) else np.nan, fill=float(fill.mean()), nf=len(f))


def table(X, label):
    rows = []
    print(f"\n{label}: {len(np.unique(X['e']))} windows, {len(X['e'])} side-samples")
    print("tau | z | r | trades (windows) | mean ask | mean mid | mean Phi(z) | win | edge_mid ± SE | edge_net ± SE | t_net | fill")
    for ti in range(4):
        for zi in range(7):
            for ri in range(3):
                c = cell_stats(X, (X["tb"] == ti) & (X["zb"] == zi) & (X["rb"] == ri))
                if c is None:
                    continue
                t_ = c["en"] / c["en_se"] if c["en_se"] and c["en_se"] == c["en_se"] and c["en_se"] > 0 else np.nan
                rows.append(dict(c, ti=ti, zi=zi, ri=ri, t=t_))
                print(f"{TL[ti]} | {ZL[zi]} | {RL[ri]} | {c['n']} ({c['windows']}) | {c['ask']:.3f} | {c['mid']:.3f} | {c['phi']:.3f} | {c['win']:.3f} | "
                      f"{100 * c['em']:+.2f} ± {100 * c['em_se']:.2f} | {100 * c['en']:+.2f} ± {100 * c['en_se']:.2f} | {t_:+.1f} | {c['fill']:.2f}")
    return rows


def cell_w(X, label):
    print(f"\nnamed cell W ({label}): d in [+5, +20] bp, tau in [120, 290], |1 s move| < 0.5 bp, ask <= mid + 1c")
    base = (X["d"] >= 5) & (X["d"] <= 20) & (X["tau"] >= 120) & (X["tau"] <= 290) & (np.abs(X["m1"]) < 0.5) & (X["ask"] <= X["mid"] + 0.01 + 1e-9)
    for ri in range(3):
        c = cell_stats(X, base & (X["rb"] == ri))
        if c:
            print(f"  r {RL[ri]}: trades {c['n']} ({c['windows']} windows), ask {c['ask']:.3f}, mid {c['mid']:.3f}, win {c['win']:.3f}, "
                  f"edge_mid {100 * c['em']:+.2f} ± {100 * c['em_se']:.2f}, edge_net {100 * c['en']:+.2f} ± {100 * c['en_se']:.2f}, fill {c['fill']:.2f}")
    c = cell_stats(X, base)
    print(f"  all r: trades {c['n']}, edge_net {100 * c['en']:+.2f} ± {100 * c['en_se']:.2f}, edge_mid {100 * c['em']:+.2f} ± {100 * c['em_se']:.2f}")


def cmd_dev(a):
    X = load("dev")
    rows = table(X, "DEV (before 07-19, without 06-01..03)")
    n = len(rows)
    print(f"\ncells: {n}; |t_net| >= 2: {sum(abs(r['t']) >= 2 for r in rows if r['t'] == r['t'])} (chance ~{0.0455 * n:.1f}); |t| >= 3: {sum(abs(r['t']) >= 3 for r in rows if r['t'] == r['t'])} (chance ~{0.0027 * n:.2f})")
    cand = []
    for r in rows:
        if not (r["t"] == r["t"] and r["t"] >= 3 and r["en"] > 0):
            continue
        nb = [q for q in rows if (abs(q["ti"] - r["ti"]) + abs(q["zi"] - r["zi"]) + abs(q["ri"] - r["ri"]) == 1) and q["en"] > 0]
        cand.append((r, len(nb)))
    print("candidates (dev edge_net > 0, t >= 3), with same-sign neighbours:")
    for r, k in cand:
        print(f"  {TL[r['ti']]} {ZL[r['zi']]} {RL[r['ri']]}: edge_net {100 * r['en']:+.2f}c t {r['t']:+.1f}, n {r['n']}, neighbours >0: {k}")
    cell_w(X, "dev")
    json.dump([dict(ti=r["ti"], zi=r["zi"], ri=r["ri"], en=r["en"], t=r["t"], n=r["n"], nb=k) for r, k in cand], open(OUT / "dev_candidates.json", "w"))


def logit(p):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


def fit(Xm, y, iters=60, ridge=1e-4):
    w = np.zeros(Xm.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xm @ w))
        g = Xm.T @ (p - y) + ridge * w
        H = (Xm * (p * (1 - p))[:, None]).T @ Xm + ridge * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w


def design(X, full):
    cols = [np.ones(len(X["mid"])), logit(X["mid"])]
    if full:
        z = np.clip(X["z"], -8, 8)
        cols += [z, z * (X["r"] < 0.7), z * (X["r"] > 1.4)]
    return np.column_stack(cols)


def cmd_holdout(a):
    D, H = load("dev"), load("holdout")
    fr = json.load(open(Path(__file__).resolve().parent.parent / "frozen" / "t3_cells.json"))
    print(f"holdout {HOLD_START}..{HOLD_END}: {len(np.unique(H['e']))} windows (NB: already used by earlier studies)")
    for c in fr["cells"]:
        st = cell_stats(H, (H["tb"] == c["ti"]) & (H["zb"] == c["zi"]) & (H["rb"] == c["ri"]))
        print(f"  frozen {TL[c['ti']]} {ZL[c['zi']]} {RL[c['ri']]}: n {st['n']}, edge_net {100 * st['en']:+.2f} ± {100 * st['en_se']:.2f}c, "
              f"edge_mid {100 * st['em']:+.2f} ± {100 * st['em_se']:.2f}c, fill {st['fill']:.2f}")
    cell_w(H, "holdout")
    # log-loss, UP side only, all 5 s samples; fit on dev, score on holdout
    for nm, X in (("dev", D), ("holdout", H)):
        m = (X["side"] == 0) & np.isfinite(X["z"]) & (X["mid"] > 0) & (X["mid"] < 1)
        for k in X:
            X[k] = X[k][m] if hasattr(X[k], "__len__") and len(X[k]) == len(m) else X[k]
    ll = {}
    for full in (False, True):
        w = fit(design(D, full), D["win"])
        p = np.clip(1 / (1 + np.exp(-design(H, full) @ w)), 1e-6, 1 - 1e-6)
        ll[full] = -(H["win"] * np.log(p) + (1 - H["win"]) * np.log(1 - p))
        print(f"  model {'logit(mid)+z+z[r<0.7]+z[r>1.4]' if full else 'logit(mid)'}: coefs {np.round(w, 3).tolist()}, holdout log-loss {ll[full].mean():.5f}")
    diff = ll[True] - ll[False]
    u, inv = np.unique(H["e"], return_inverse=True)
    per = np.bincount(inv, diff) / np.bincount(inv)
    rng = np.random.default_rng(3)
    bs = [per[rng.integers(0, len(per), len(per))].mean() for _ in range(2000)]
    print(f"  log-loss change (full - mid only): {diff.mean():+.5f} per sample; per-window mean {per.mean():+.5f}, 90% [{np.percentile(bs, 5):+.5f}, {np.percentile(bs, 95):+.5f}]")
    # favourites by 6-hour UTC block (both splits shown; one trade per window per block-side as in cells)
    for nm, X in (("dev", load("dev")), ("holdout", load("holdout"))):
        hr = (X["e"] % 86400) // 3600
        print(f"  favourites (z > 0.3) edge_mid by 6-h UTC block, {nm}:")
        for b in range(4):
            c = cell_stats(X, (X["z"] > 0.3) & (hr // 6 == b))
            print(f"    {6 * b:02d}-{6 * b + 6:02d}h: n {c['n']}, mid {c['mid']:.3f}, win {c['win']:.3f}, edge_mid {100 * c['em']:+.2f} ± {100 * c['em_se']:.2f}c, "
                  f"edge_net {100 * c['en']:+.2f} ± {100 * c['en_se']:.2f}c")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("extract")
    p.add_argument("--days", nargs="*", default=None)
    p.add_argument("--out", default=None)
    for c in ("dev", "holdout"):
        sp.add_parser(c)
    a = ap.parse_args()
    {"extract": cmd_extract, "dev": cmd_dev, "holdout": cmd_holdout}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
