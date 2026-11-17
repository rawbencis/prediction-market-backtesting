"""Momentum taker on shadow recordings (shadow_rec.py): the OhioAI-style rule, frozen 2026-11-14 before reading
any shadow data with it.

Rule (per market, decision grid every 50 ms on the recorder's clock, Binance price as RECEIVED by then):
  mom = 1e4 * ln(S(t) / S(t - WIN))  ->  side UP if mom >= M, DOWN if mom <= -M
  only while 30..300 s are left in the window, and the side's ask as seen is 0.20..0.80
  FAK buy 5 shares, limit = seen ask + CAP; fills at the ask standing when the order matches if <= limit
  and >= 5 shares show. Timing scenarios exactly as shadow_eval (tokyo, colo-X, exchange taker delay).
Modes: first = first fill per market; multi = after each fill, 10 s cooldown, up to 5 fills per market.

PRIMARY (the only pre-registered test): BTC 5m, spot, tokyo, M=2 bp, WIN=5 s, CAP=2c, first.
  Pass: >= 14 settled days, >= 500 fills, net >= +1.0 c/share, day-bootstrap 90% CI lower bound > 0.
  Kill: CI upper bound < +1.0 c/share after 14 days.
Everything else (other assets, 15m, futures signal, M/WIN/CAP grid, multi, colo) is exploratory.
"""

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import shadow_eval as se
import shadow_rec as rec
from leadlag import boot, fee

STEP_US = 50_000
MS = (1.0, 2.0, 3.0, 5.0)
WINS = (2.0, 5.0)
CAPS = (0.02, 0.05)
LO, HI = 0.20, 0.80
LEFT = (30, 300)
PRIMARY = dict(asset="btc", tf=5, src="spot", scen="tokyo", M=2.0, win=5.0, cap=0.02, mode="first")
COLS = ["day", "asset", "tf", "epoch", "src", "scen", "M", "win", "cap", "mode", "n", "filled", "side", "t_s", "mom", "seen", "px", "asz"]


def fills_for(sig_side, seen, arr, asz, cap, mode, g, cool_us=10_000_000, nmax=5):
    """sig_side: +1 UP / -1 DOWN / 0 per grid point. seen/arr/asz: dict side -> arrays.
    -> list of (index, side) fills, and whether any signal occurred."""
    out, any_sig, nxt = [], False, -1
    for side, sv in ((1, "UP"), (-1, "DOWN")):
        s = sig_side == side
        v = seen[sv]
        s &= np.nan_to_num(v, nan=-1) >= LO
        s &= np.nan_to_num(v, nan=9) <= HI
        any_sig |= bool(s.any())
        ok = s & (np.nan_to_num(arr[sv], nan=9) <= v + cap + 1e-9) & (asz[sv] >= se.SIZE)
        for i in np.flatnonzero(ok):
            out.append((int(i), sv))
    out.sort()
    if mode == "first":
        return out[:1], any_sig
    res = []
    for i, sv in out:
        if g[i] >= nxt and len(res) < nmax:
            res.append((i, sv))
            nxt = g[i] + cool_us
    return res, any_sig


def eval_day(root, day, a):
    files = se.pm_files(root, day)
    q = se.lag_pct(files, [10, 50])
    prev = (date.fromisoformat(day) - timedelta(1)).isoformat()
    bn = np.concatenate([se.read(root, prev, "bn-23", se.BN_DT), se.read(root, day, "bn-", se.BN_DT)])
    if q is None or not len(bn):
        return None
    d10, d50 = q
    scen = [("tokyo", 0.0, d50, d10 + a.proc_ms)] + [(f"colo-{int(s)}", d10 - s, 1.0, 1.0 + a.proc_ms) for s in a.relay_save_ms]
    B = {}
    for s in (0, 1):
        m = bn["src"] == s
        for i in range(len(rec.ASSETS)):
            x = bn[m & (bn["a"] == i)]
            x = x[np.argsort(x["l"], kind="stable")]
            B[s, i] = (x["l"].copy(), x["p"].copy())
    del bn, x
    # rows go to disk one window at a time (a full day is ~1-2 M rows: too big to hold as tuples);
    # windows arrive in time order, so the file is ordered by window close, not by (asset, tf, epoch)
    st, n, path = {}, 0, a.res / f"mom-{day}.csv"
    tmp = path.with_suffix(".part")
    pd.DataFrame(columns=COLS).to_csv(tmp, index=False)
    for r in se.iter_windows(files, stats=st):
        rows = window_rows(r[np.argsort(r["x"], kind="stable")], B, scen, day, a)
        if rows:
            pd.DataFrame(rows, columns=COLS).to_csv(tmp, mode="a", header=False, index=False)
            n += len(rows)
    tmp.replace(path)
    return dict(day=day, rows=n, d10=round(float(d10), 1), d50=round(float(d50), 1), late_rows=st["late_rows"])


def window_rows(r, B, scen, day, a):
    rows = []
    i, tf, ep = int(r["a"][0]), int(r["tf"][0]), int(r["e"][0])
    W, e_us = tf * 60, ep * 1_000_000
    M = {"xu": r["x"] * 1000, "b": r["b"], "k": r["k"], "bs": r["bs"], "ks": r["ks"]}
    g = np.arange(max(5, W - LEFT[1]) * 1_000_000, (W - LEFT[0]) * 1_000_000, STEP_US)
    t = e_us + g
    books = {}
    for n, ad, sv, b in scen:
        v = se.book_at(M, t + int((ad - sv) * 1000))
        w = se.book_at(M, t + int((ad + b + a.taker_delay_ms) * 1000))
        books[n] = ({"UP": v[0], "DOWN": v[2]}, {"UP": w[0], "DOWN": w[2]}, {"UP": w[1], "DOWN": w[3]})
    for s in (0, 1):
        L, P = B[s, i]
        if len(L) < 2:
            continue
        for win in WINS:
            p1 = P[np.maximum(np.searchsorted(L, t, "right") - 1, 0)]
            p0 = P[np.maximum(np.searchsorted(L, t - int(win * 1e6), "right") - 1, 0)]
            valid = (np.searchsorted(L, t - int(win * 1e6), "right") > 0)
            mom = np.where(valid, 1e4 * np.log(p1 / p0), 0.0)
            for Mv in MS:
                side = np.where(mom >= Mv, 1, np.where(mom <= -Mv, -1, 0))
                if not side.any():
                    continue
                for name, (seen, arr, asz) in books.items():
                    for cap in CAPS:
                        for mode in ("first", "multi"):
                            f, anys = fills_for(side, seen, arr, asz, cap, mode, g)
                            base = (day, i, tf, ep, s, name, Mv, win, cap, mode)
                            for n, (j, sv) in enumerate(f):
                                rows.append(base + (n, 1, sv, g[j] / 1e6, float(mom[j]), float(seen[sv][j]), float(arr[sv][j]), float(asz[sv][j])))
                            if anys and not f:
                                rows.append(base + (0, 0, "", np.nan, np.nan, np.nan, np.nan, np.nan))
    return rows


def cmd_eval(a):
    a.res.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    for d in a.days or sorted(p.name for p in a.rec.glob("????-??-??") if p.is_dir()):
        # only finished days: a day evaluated at 00:25 while still recording would be kept truncated forever
        if d < a.since or d >= today or ((a.res / f"mom-{d}.csv").exists() and not a.force):
            continue
        print(eval_day(a.rec, d, a), flush=True)


SCENS = pd.CategoricalDtype(["tokyo", "colo-0", "colo-10", "colo-20"])
SIDES = pd.CategoricalDtype(["UP", "DOWN"])
RDT = {"asset": "int8", "tf": "int8", "epoch": "int64", "src": "int8", "scen": SCENS, "M": "float64", "win": "float64",
       "cap": "float64", "mode": pd.CategoricalDtype(["first", "multi"]), "filled": "int8", "side": SIDES, "px": "float64"}  # fmt: skip


def cmd_report(a):
    """Streams the result files (a day is ~1-2 M rows): per-config sums are added up chunk by chunk;
    only the primary's and the BTC 5m uplift rows are kept."""
    files = [f for f in sorted(a.res.glob("mom-*.csv")) if f.stem[4:] >= a.since]
    U = set()
    for f in files:
        for c in pd.read_csv(f, usecols=["asset", "tf", "epoch"], dtype="int64", chunksize=1_000_000):
            U.update(map(tuple, c.drop_duplicates().to_numpy().tolist()))
    slug = {u: f"{rec.ASSETS[u[0]]}-updown-{u[1]}m-{u[2]}" for u in U}
    W = se.settle(a.res / "settlements.csv", sorted(set(slug.values())), not a.no_fetch)
    win = {u[0] * 2**40 + u[1] * 2**32 + u[2]: W[s] for u, s in slug.items() if s in W}
    keys = ["asset", "tf", "src", "scen", "M", "win", "cap", "mode"]
    agg, Ps, Ss, days = [], [], [], []
    for f in files:
        day = f.stem[4:]
        for c in pd.read_csv(f, usecols=list(RDT), dtype=RDT, chunksize=500_000):
            c["w"] = (c.asset.astype(np.int64) * 2**40 + c.tf.astype(np.int64) * 2**32 + c.epoch).map(win)
            c = c[c.w.notna()]
            if not len(c):
                continue
            if day not in days:
                days.append(day)
            F = c[c.filled == 1].copy()
            F["asset"] = pd.Categorical.from_codes(F.asset, categories=list(rec.ASSETS))
            F["src"] = pd.Categorical.from_codes(F.src, categories=list(se.SRC))
            F["pps"] = (F.side.astype(object) == F.w).astype(float) - F.px - fee(F.px, a.fee_rate)
            F["day"] = day
            agg.append(F.groupby(keys, observed=True).pps.agg(["size", "sum"]))
            Ps.append(F[np.logical_and.reduce([F[k] == v for k, v in PRIMARY.items()])][["day", "pps"]])
            m = (F.asset == "btc") & (F.tf == 5) & (F.src == "spot") & (F.M == 2.0) & (F.win == 5.0) & (F.cap == 0.02) & (F["mode"] == "first")
            Ss.append(F[m][["day", "scen", "pps"]])
    days = sorted(days)
    if not days:
        print(f"no settled markets yet in {len(files)} result files")
        return
    nd = len(days)
    print(f"settled days {days[:1]}..{days[-1:]} ({nd}), markets {len(win)}")
    G = pd.concat(agg).groupby(level=keys, observed=True).sum()
    G = pd.DataFrame({"fills_day": (G["size"] / nd).round(1), "c_share": (100 * G["sum"] / G["size"]).round(2),
                      "usd_day_5sh": (G["sum"] * se.SIZE / nd).round(2)}).reset_index()  # fmt: skip
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 400)
    base = G[(G["mode"] == "first") & (G.cap == PRIMARY["cap"]) & (G.win == PRIMARY["win"])]
    print("\nfirst fill per market, WIN 5 s, CAP 2c (all assets/tf/src/scen/M):\n", base.to_string(index=False))
    P = pd.concat(Ps, ignore_index=True)
    D = P.groupby("day").pps.mean().reindex(days)
    m, lo, hi = boot(P.groupby("day").pps.mean().to_numpy() * 100)
    pooled = 100 * P.pps.mean() if len(P) else np.nan
    print(f"\nPRIMARY {PRIMARY}\n  fills {len(P)} over {nd} days; net {pooled:+.2f} c/share; day-bootstrap mean {m:+.2f} [90% CI {lo:+.2f}, {hi:+.2f}]")
    verdict = "RUNNING"
    if nd >= 14 and len(P) >= 500 and pooled >= 1.0 and lo > 0:
        verdict = "PASS"
    elif nd >= 14 and hi < 1.0:
        verdict = "KILL"
    print(f"  verdict: {verdict}  (pass: >=14 days, >=500 fills, net >= +1.0c, CI lb > 0; kill: CI ub < +1.0c after 14 days)")
    print("  by day (c/share):", (100 * D).round(2).to_dict())
    S = pd.concat(Ss, ignore_index=True)
    T = S.pivot_table(index="day", columns="scen", values="pps", aggfunc="sum", observed=True).reindex(days).fillna(0) * se.SIZE
    for c in [str(c) for c in T.columns if c != "tokyo"]:
        mm, l2, h2 = boot((T[c] - T["tokyo"]).to_numpy())
        print(f"  uplift {c} vs tokyo: {mm:+.2f} $/day [{l2:+.2f}, {h2:+.2f}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("eval")
    p.add_argument("--rec", type=Path, required=True)
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--days", nargs="*")
    p.add_argument("--since", default="2026-11-14")
    p.add_argument("--taker-delay-ms", type=float, default=50.0)
    p.add_argument("--proc-ms", type=float, default=10.0)
    p.add_argument("--relay-save-ms", type=float, nargs="*", default=[0.0, 10.0, 20.0])
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--since", default="2026-11-14")
    p.add_argument("--fee-rate", type=float, default=0.07)
    p.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    {"eval": cmd_eval, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
