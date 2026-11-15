"""Minimal speed atlas (stand-in for the missing speed_atlas.py): Binance >= M bp moves vs Polymarket book reaction.
Event: Binance trade at T where |p / p(T - 500 ms) - 1| >= M bp, 2 s debounce per asset. For every live window of that
asset (mid 0.05..0.95, >= 10 s left): reprice delay = first book row after T whose mid moved >= half a tick in the move
direction (exchange ms - Binance ms, both venue clocks); stale = the ask toward the move at T, hittable at T + d if the
book (strictly before T + d) still offers <= that price with >= 5 shown.
Usage: python atlas.py --day 2026-11-14 --src spot|fut [--edge]
--edge: taker edge at landing. Per event x live window, buy the side the move favours with a FAK landing at t0 + L
(book standing then, >= 5 shown, same-ms prints first); edge_L = mid(t0 + 2 s) - price - 0.07 p (1 - p) in cents, and
the same against the settlement winner. Plus an alt-led BTC 5m trigger. Descriptive only, 2026-11-14 only.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d

DELAYS = (130, 160, 200)
LANDS = (100, 130, 165, 200, 300)
MOVE_B = ((2, 5, "2-5bp"), (5, 10, "5-10bp"), (10, 1e9, "10+bp"))
TL_B = ((30, 120, "30-120s"), (120, 300, "120-300s"), (300, 1e9, "300+s"))


def events(bn, src, ai, m_bp):
    b = bn[(bn["src"] == src) & (bn["a"] == ai)]
    b = b[np.argsort(b["T"], kind="stable")]
    T, p = b["T"], b["p"]
    j = np.searchsorted(T, T - 500, "left")
    r = (p / p[j] - 1) * 1e4
    out, last = [], -10**15
    for i in np.flatnonzero(np.abs(r) >= m_bp):
        if T[i] - last >= 2000:
            out.append((int(T[i]), int(np.sign(r[i])), float(r[i])))
            last = T[i]
    return out


def react(m, t0, sgn):
    B = m["book"]
    i = d.state(m, t0)
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return None
    mid0 = (int(B["b"][i]) + int(B["k"][i])) / 2
    if not 500 <= mid0 <= 9500:
        return None
    # stale ask toward the move: up -> UP ask; down -> DOWN ask = 1 - UP bid (kept in UP terms: the UP bid)
    lvl = int(B["k"][i]) if sgn > 0 else int(B["b"][i])
    j1 = np.searchsorted(B["x"], t0 + 10_000, "left")
    s = slice(i + 1, j1)
    bb, kk, xx = B["b"][s].astype(int), B["k"][s].astype(int), B["x"][s]
    mids = (bb + kk) / 2
    ok = (bb > 0) & (kk > 0) & ((mids - mid0) * sgn >= 50)
    delay = float(xx[np.argmax(ok)] - t0) if ok.any() else np.nan
    row = dict(delay=delay)
    for dt in DELAYS:
        k = d.state(m, t0 + dt)
        if B["b"][k] == 0 and B["k"][k] == 0:
            row[f"hit{dt}"] = np.nan
            continue
        if sgn > 0:
            row[f"hit{dt}"] = float(B["k"][k] != 0 and B["k"][k] <= lvl and B["ks"][k] >= 5)
        else:
            row[f"hit{dt}"] = float(B["b"][k] != 0 and B["b"][k] >= lvl and B["bs"][k] >= 5)
    return row


def run(day, src, m_bp=2.0):
    mk, _, bn = d.load_day(day, shift=0)
    rows = []
    for ai, a in enumerate(d.ASSETS):
        for t0, sgn, r in events(bn, 0 if src == "spot" else 1, ai, m_bp):
            for tf in (5, 15):
                e = (t0 // 1000) // (tf * 60) * (tf * 60)
                m = mk.get((a, tf, e))
                if m is None or e + tf * 60 - t0 / 1000 < 10:
                    continue
                x = react(m, t0, sgn)
                if x:
                    rows.append(dict(asset=a, tf=tf, e=e, t0=t0, sgn=sgn, move_bp=r, **x))
    return pd.DataFrame(rows)


def bucket(v, B):
    for lo, hi, name in B:
        if lo <= v < hi:
            return name
    return None


def land_edges(m, t0, tok, winner):
    """-> {hit_L, edge_L, settle_L} for every landing delay L (cents per share; NaN when not hittable)."""
    row = {}
    mid2 = d.mid(m, t0 + 2000, tok)
    pay = None if winner is None else (100.0 if (winner == "UP") == (tok == 0) else 0.0)
    for L in LANDS:
        f = d.taker(m, t0 + L, tok, 9999, 5.0)
        ok = f is not None and mid2 is not None
        fee_c = 100 * d.fee(f["px"], 1.0) if f else 0.0
        row[f"hit_{L}"] = float(ok)
        row[f"edge_{L}"] = (mid2 / 100 - f["px"] / 100 - fee_c) if ok else np.nan
        row[f"settle_{L}"] = (pay - f["px"] / 100 - fee_c) if ok and pay is not None else np.nan
    return row


def live(m, t0):
    B = m["book"]
    i = d.state(m, t0)
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return False
    return 500 <= (int(B["b"][i]) + int(B["k"][i])) / 2 <= 9500


def run_edge(day, src, mk, bn, W, m_bp=2.0):
    rows = []
    for ai, a in enumerate(d.ASSETS):
        for t0, sgn, r in events(bn, 0 if src == "spot" else 1, ai, m_bp):
            for tf in (5, 15):
                e = (t0 // 1000) // (tf * 60) * (tf * 60)
                m = mk.get((a, tf, e))
                left = e + tf * 60 - t0 / 1000
                tl = bucket(left, TL_B)
                if m is None or tl is None or not live(m, t0):
                    continue
                tok = 0 if sgn > 0 else 1
                rows.append(dict(asset=a, tf=tf, e=e, t0=t0, sgn=sgn, move_bp=r, bucket=bucket(abs(r), MOVE_B), tl=tl,
                                 **land_edges(m, t0, tok, W.get(d.slug((a, tf, e))))))
    return pd.DataFrame(rows)


def alt_led(mk, W, win_ms=300, debounce=2000):
    """BTC 5m entries when >= 2 alt 5m books moved their mid >= half a tick the same way within win_ms and the BTC 5m
    book has not moved that way in the same span. t0 = the book event that completes the trigger (exchange ms)."""
    rows = []
    for e in sorted({k[2] for k in mk if k[1] == 5 and k[0] == "btc"}):
        mb = mk.get(("btc", 5, e))
        ev = []  # (x, dir, asset)
        for a in d.ASSETS:
            m = mk.get((a, 5, e))
            if m is None:
                continue
            B = m["book"]
            ok = (B["b"] > 0) & (B["k"] > 0)
            x, mid = B["x"][ok], (B["b"][ok].astype(int) + B["k"][ok].astype(int)) / 2
            dm = np.diff(mid)
            for j in np.flatnonzero(np.abs(dm) >= 50):
                ev.append((int(x[j + 1]), int(np.sign(dm[j])), a))
        ev.sort()
        last = -10**15
        for k, (t, s, a) in enumerate(ev):
            if a == "btc" or t - last < debounce or t < e * 1000:  # inside the window only (books open 45 s early)
                continue
            span = [(t2, s2, a2) for t2, s2, a2 in ev[max(0, k - 400): k + 1] if t - win_ms < t2 <= t and s2 == s]
            alts = {a2 for _, _, a2 in span if a2 != "btc"}
            if len(alts) < 2 or any(a2 == "btc" for _, _, a2 in span):
                continue
            left = e + 300 - t / 1000
            tl = bucket(left, TL_B)
            if tl is None or not live(mb, t):
                continue
            last = t
            rows.append(dict(asset="btc", tf=5, e=e, t0=t, sgn=s, bucket=f"{min(len(alts), 3)}{'+' if len(alts) >= 3 else ''} alts",
                             tl=tl, **land_edges(mb, t, 0 if s > 0 else 1, W.get(d.slug(("btc", 5, e))))))
    return pd.DataFrame(rows)


def boot_ev(v, cl, B=1000, seed=3):
    """Mean of v (EV per attempt) with a cluster (market window) bootstrap 90% CI."""
    u, inv = np.unique(cl, return_inverse=True)
    S, N = np.bincount(inv, v, len(u)), np.bincount(inv, None, len(u))
    idx = np.random.default_rng(seed).integers(0, len(u), (B, len(u)))
    b = S[idx].sum(1) / np.maximum(N[idx].sum(1), 1)
    return float(v.mean()), float(np.percentile(b, 5)), float(np.percentile(b, 95))


def edge_table(R, keys=("asset", "tf", "bucket", "tl")):
    out = []
    for g, G in R.groupby(list(keys)):
        for L in LANDS:
            h = G[f"hit_{L}"].values
            ev = np.where(h > 0, G[f"edge_{L}"].fillna(0).values, 0.0)
            sv = np.where(h > 0, G[f"settle_{L}"].fillna(0).values, 0.0)
            m, lo, hi = boot_ev(ev, G.e.values)
            ms, slo, shi = boot_ev(sv, G.e.values)
            out.append(dict(zip(keys, g), L=L, n=len(G), hit=round(h.mean(), 3),
                            edge_hit=round(float(G[f"edge_{L}"][h > 0].mean()), 2) if h.any() else np.nan,
                            ev=round(m, 3), ev_lo=round(lo, 3), ev_hi=round(hi, 3),
                            settle_hit=round(float(G[f"settle_{L}"][h > 0].mean()), 2) if h.any() else np.nan,
                            ev_settle=round(ms, 3), evs_lo=round(slo, 3), evs_hi=round(shi, 3)))
    return pd.DataFrame(out)


def edge_report(T, title, min_n=30):
    lines = [f"== {title}", "EV per attempt in cents/share (edge if hittable else 0), mid(t0+2s) markout; [90% CI market bootstrap];"
             " settle = vs Gamma winner. Columns per landing L ms: hit% / EV"]
    W_ = T.pivot_table(index=["asset", "tf", "bucket", "tl", "n"], columns="L", values=["hit", "ev"])
    for idx, r in W_.iterrows():
        cells = "  ".join(f"L{L}: {100 * r[('hit', L)]:3.0f}% {r[('ev', L)]:+6.2f}" for L in LANDS)
        lines.append(f"  {idx[0]:4s} {idx[1]:>2}m {idx[2]:8s} {idx[3]:8s} n={idx[4]:5d}  {cells}")
    for tf in (5, 15):
        C = T[(T.asset == "btc") & (T.tf == tf) & (T.n >= min_n)]
        if len(C):
            b = C.loc[C.ev.idxmax()]
            lines.append(f"  BEST btc {tf}m (n >= {min_n}; best of {len(C)} cells, selection-biased): {b.bucket} {b.tl} L={b.L} n={b.n} "
                         f"hit {100 * b.hit:.0f}% edge|hit {b.edge_hit:+.2f}c EV {b.ev:+.3f}c [{b.ev_lo:+.3f}, {b.ev_hi:+.3f}]"
                         f"  settle EV {b.ev_settle:+.2f}c [{b.evs_lo:+.2f}, {b.evs_hi:+.2f}]")
    return "\n".join(lines)


def summary(R, src):
    out = [f"== src={src}: events x live windows n={len(R)} (>= 2 bp in 500 ms, 2 s debounce)"]
    g = R.groupby(["asset", "tf"])
    t = pd.DataFrame({"n": g.size(), "repriced_10s": g.delay.apply(lambda s: s.notna().mean()).round(2),
                      "delay_p25": g.delay.quantile(0.25), "delay_p50": g.delay.median(), "delay_p75": g.delay.quantile(0.75)})
    for dt in DELAYS:
        t[f"stale@{dt}"] = g[f"hit{dt}"].mean().round(3)
    out.append("(a)+(b) per asset x tf: reprice delay ms (Binance trade time -> PM book exchange time) and share of events"
               " where the stale ask toward the move is still hittable (>= 5 shown) at t0 + d")
    out.append(t.round(1).to_string())
    tot = {f"stale@{dt}": round(float(R[f'hit{dt}'].mean()), 3) for dt in DELAYS}
    out.append(f"all: delay p50 {R.delay.median():.0f} ms, {tot}")
    # (c) leadership
    P = R.pivot_table(index=["asset", "t0"], columns="tf", values="delay")
    if 5 in P and 15 in P:
        both = P.dropna()
        out.append(f"(c1) 5m vs 15m same asset+event: n={len(both)}, 5m first {np.mean(both[5] < both[15]):.2f}, "
                   f"tie {np.mean(both[5] == both[15]):.2f}, median(15m - 5m) {np.median(both[15] - both[5]):.0f} ms")
    R5 = R[R.tf == 5]
    btc = R5[R5.asset == "btc"].set_index("t0").delay
    for a in ("eth", "sol", "xrp", "doge"):
        alt = R5[R5.asset == a].set_index("t0").delay
        # pair each BTC event with the alt event within 300 ms (same market move)
        bt, at = btc.dropna(), alt.dropna()
        if not len(bt) or not len(at):
            continue
        ai = np.searchsorted(at.index.values, bt.index.values)
        pairs = []
        for k, t in enumerate(bt.index.values):
            for c in (ai[k] - 1, ai[k]):
                if 0 <= c < len(at) and abs(at.index.values[c] - t) <= 300:
                    pairs.append((t + bt.values[k], at.index.values[c] + at.values[c]))
                    break
        if pairs:
            pr = np.array(pairs)
            out.append(f"(c2) BTC vs {a.upper()} 5m books, same move (Binance events within 300 ms): n={len(pr)}, BTC book "
                       f"reprices first {np.mean(pr[:, 0] < pr[:, 1]):.2f}, median(alt - BTC) reprice time "
                       f"{np.median(pr[:, 1] - pr[:, 0]):.0f} ms")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-14")
    ap.add_argument("--src", default="spot", choices=("spot", "fut"))
    ap.add_argument("--out", default=str(Path.home() / "dublin-retest" / "out" / "atlas"))
    ap.add_argument("--edge", action="store_true")
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    if a.edge:
        if a.day != "2026-11-14":
            raise SystemExit("taker-edge numbers: 2026-11-14 only")
        mk = d.load_markets(a.day)
        bn = d.read(d.REC, a.day, "bn", d.BN_DT, lambda x: x["src"] == (0 if a.src == "spot" else 1))
        W = d.winners()
        R = run_edge(a.day, a.src, mk, bn, W)
        R.to_csv(Path(a.out) / f"edge-{a.day}-{a.src}-events.csv.gz", index=False)
        T = edge_table(R)
        T.to_csv(Path(a.out) / f"edge-{a.day}-{a.src}.csv", index=False)
        print(edge_report(T, f"taker edge at landing, src={a.src}, Binance >= 2 bp / 500 ms, {a.day}"))
        if a.src == "spot":  # book-only trigger: independent of src, printed once
            A = alt_led(mk, W)
            A.to_csv(Path(a.out) / f"edge-{a.day}-altled-events.csv.gz", index=False)
            TA = edge_table(A)
            TA.to_csv(Path(a.out) / f"edge-{a.day}-altled.csv", index=False)
            print(edge_report(TA, f"alt-led BTC 5m entry (>= 2 alt 5m books moved, BTC 5m not, within 300 ms), {a.day}"))
        raise SystemExit
    R = run(a.day, a.src)
    R.to_csv(Path(a.out) / f"atlas-{a.day}-{a.src}.csv", index=False)
    print(summary(R, a.src))
