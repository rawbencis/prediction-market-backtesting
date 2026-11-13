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
    pm = se.read(root, day, "pm-", se.PM_DT)
    prev = (date.fromisoformat(day) - timedelta(1)).isoformat()
    bn = np.concatenate([se.read(root, prev, "bn-23", se.BN_DT), se.read(root, day, "bn-", se.BN_DT)])
    if not len(pm) or not len(bn):
        return None
    live = (pm["x"] > 0) & (pm["k"] > 0)
    d10, d50 = np.percentile(pm["l"][live] / 1000 - pm["x"][live], [10, 50])
    scen = [("tokyo", 0.0, d50, d10 + a.proc_ms)] + [(f"colo-{int(s)}", d10 - s, 1.0, 1.0 + a.proc_ms) for s in a.relay_save_ms]
    B = {}
    for s in (0, 1):
        m = bn["src"] == s
        for i in range(len(rec.ASSETS)):
            x = bn[m & (bn["a"] == i)]
            x = x[np.argsort(x["l"], kind="stable")]
            B[s, i] = (x["l"], x["p"])
    key = (pm["a"].astype(np.int64) << 40) | (pm["tf"].astype(np.int64) << 32) | pm["e"]
    o = np.lexsort((pm["x"], key))
    pm, key = pm[o], key[o]
    cuts = np.r_[0, np.flatnonzero(np.diff(key)) + 1, len(pm)]
    rows = []
    for c0, c1 in zip(cuts[:-1], cuts[1:]):
        r = pm[c0:c1]
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
    pd.DataFrame(rows, columns=COLS).to_csv(a.res / f"mom-{day}.csv", index=False)
    return dict(day=day, rows=len(rows), d10=round(float(d10), 1), d50=round(float(d50), 1))


def cmd_eval(a):
    a.res.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    for d in a.days or sorted(p.name for p in a.rec.glob("????-??-??") if p.is_dir()):
        if d < a.since or ((a.res / f"mom-{d}.csv").exists() and d < today and not a.force):
            continue
        print(eval_day(a.rec, d, a), flush=True)


def cmd_report(a):
    O = pd.concat([pd.read_csv(f) for f in sorted(a.res.glob("mom-*.csv")) if f.stem[4:] >= a.since], ignore_index=True)
    O["asset"] = O.asset.map(dict(enumerate(rec.ASSETS)))
    O["src"] = O.src.map(dict(enumerate(se.SRC)))
    O["slug"] = O.asset + "-updown-" + O.tf.astype(str) + "m-" + O.epoch.astype(str)
    W = se.settle(a.res / "settlements.csv", sorted(set(O.slug)), not a.no_fetch)
    O["w"] = O.slug.map(W)
    O = O[O.w.notna()]
    days = sorted(O.day.unique())
    nd = max(len(days), 1)
    F = O[O.filled == 1].copy()
    F["pps"] = (F.side == F.w).astype(float) - F.px - fee(F.px, a.fee_rate)
    print(f"settled days {days[:1]}..{days[-1:]} ({nd}), markets {O.slug.nunique()}")
    keys = ["asset", "tf", "src", "scen", "M", "win", "cap", "mode"]
    G = F.groupby(keys).pps.agg(["size", "mean", "sum"])
    G = pd.DataFrame({"fills_day": (G["size"] / nd).round(1), "c_share": (100 * G["mean"]).round(2),
                      "usd_day_5sh": (G["sum"] * se.SIZE / nd).round(2)}).reset_index()  # fmt: skip
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 400)
    base = G[(G.mode == "first") & (G.cap == PRIMARY["cap"]) & (G.win == PRIMARY["win"])]
    print("\nfirst fill per market, WIN 5 s, CAP 2c (all assets/tf/src/scen/M):\n", base.to_string(index=False))
    P = F[np.logical_and.reduce([F[k] == v for k, v in PRIMARY.items()])]
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
    S = F[(F.asset == "btc") & (F.tf == 5) & (F.src == "spot") & (F.M == 2.0) & (F.win == 5.0) & (F.cap == 0.02) & (F["mode"] == "first")]
    T = S.pivot_table(index="day", columns="scen", values="pps", aggfunc="sum").reindex(days).fillna(0) * se.SIZE
    for c in [c for c in T.columns if c != "tokyo"]:
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
