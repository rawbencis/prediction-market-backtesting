"""Evaluate shadow recordings (shadow_rec.py): what a taker lead-lag bot would have filled, by location.

eval:   per recorded day and market. Decision times every 100 ms on the recorder's clock. Fair value
        p_up = N(k * ln(S/S0) / (sig * sqrt(tau))) from the Binance price the recorder had RECEIVED by then
        (spot and USD-M evaluated separately; S0 = last trade before the open, sig = trailing 30 min).
        Scenarios, all in Polymarket exchange time:
          tokyo   decide on receipt, seeing a book d50 ms old; order reaches the CLOB d10 + proc ms later.
          colo-X  a box next to the CLOB fed by a relay from Tokyo that is X ms faster than the public
                  path (relay = d10 - X): decides relay ms later on a 1 ms old book, order lands 1 + proc
                  ms after that.
        d10 / d50 are that day's measured Polymarket-to-recorder delays. Every order then waits the
        exchange's taker delay before matching. A signal is fair - ask - fee > theta on the book the bot
        sees; the order (limit at fair - theta net of fee) fills at the ask standing when it matches if
        the edge is still >= theta and >= SIZE shares show. First fill per market, held to settlement.
report: winners from Gamma, then $ per day by asset / timeframe / scenario, the colocation uplift
        (paired by day), theta sensitivity, and the best-fitting model scale k per asset.
"""

import argparse
import csv
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import shadow_rec as rec
from leadlag import boot, fee, loglik, ndtr

PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])  # fmt: skip
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
assert PM_DT.itemsize == rec.PM_REC.size and BN_DT.itemsize == rec.BN_REC.size
SRC = ("spot", "fut")
SIZE = 5.0
VOL_WIN = 1800
STEP_US = 100_000
THETAS = (0.03, 0.05, 0.08, 0.12)
ZFRAC = (0.2, 0.4, 0.6, 0.8)
OCOLS = ["day", "asset", "tf", "epoch", "src", "scen", "theta", "filled", "side", "t_s", "fair", "px", "asz"]
ZCOLS = ["day", "asset", "tf", "epoch", "src", "frac", "z", "mid"]


def read(root, day, kind, dt):
    parts = [
        np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
        for f in sorted((root / day).glob(f"{kind}*.bin"))
    ]
    return np.concatenate(parts) if parts else np.zeros(0, dt)


def book_at(M, q):
    """Book at exchange times q (us) -> UP ask, UP ask size, DOWN ask (1 - UP bid), DOWN ask size."""
    i = np.searchsorted(M["xu"], q, "right") - 1
    j = np.maximum(i, 0)
    b, k = M["b"][j].astype(float), M["k"][j].astype(float)
    ok = (i >= 0) & (b < k)
    return (np.where(ok & (k > 0), k / 1e4, np.nan), M["ks"][j],
            np.where(ok & (b > 0), 1 - b / 1e4, np.nan), M["bs"][j])  # fmt: skip


def first_fill(fair, view, arr, asz, theta, rate):
    """-> (any signal, index of the first signal that fills or -1)."""
    sig = np.nan_to_num(fair - view - fee(view, rate), nan=-9) > theta
    ok = sig & (np.nan_to_num(fair - arr - fee(arr, rate), nan=-9) >= theta) & (asz >= SIZE)
    return bool(sig.any()), (int(np.argmax(ok)) if ok.any() else -1)


def eval_day(root, day, a):
    pm = read(root, day, "pm-", PM_DT)
    prev = (date.fromisoformat(day) - timedelta(1)).isoformat()
    bn = np.concatenate([read(root, prev, "bn-23", BN_DT), read(root, day, "bn-", BN_DT)])
    if not len(pm) or not len(bn):
        return None
    live = (pm["x"] > 0) & (pm["k"] > 0)
    d10, d50, d90 = np.percentile(pm["l"][live] / 1000 - pm["x"][live], [10, 50, 90])
    scen = [("tokyo", 0.0, d50, d10 + a.proc_ms)]
    scen += [(f"colo-{int(s)}", d10 - s, 1.0, 1.0 + a.proc_ms) for s in a.relay_save_ms]
    B, lat = {}, {"pm_ms": [round(float(v), 1) for v in (d10, d50, d90)]}
    for s in (0, 1):
        m = bn["src"] == s
        if m.any():
            lat[f"{SRC[s]}_ms"] = [
                round(float(v), 1) for v in np.percentile(bn["l"][m] / 1000 - bn["T"][m], [10, 50, 90, 99])
            ]
        for i in range(len(rec.ASSETS)):
            x = bn[m & (bn["a"] == i)]
            x = x[np.argsort(x["l"], kind="stable")]
            B[s, i] = (x["l"], np.maximum.accumulate(x["T"]) if len(x) else x["T"], x["p"])
    key = (pm["a"].astype(np.int64) << 40) | (pm["tf"].astype(np.int64) << 32) | pm["e"]
    o = np.lexsort((pm["x"], key))
    pm, key = pm[o], key[o]
    cuts = np.r_[0, np.flatnonzero(np.diff(key)) + 1, len(pm)]
    orders, zrows, nm = [], [], 0
    for c0, c1 in zip(cuts[:-1], cuts[1:]):
        r = pm[c0:c1]
        i, tf, ep = int(r["a"][0]), int(r["tf"][0]), int(r["e"][0])
        W, e_us = tf * 60, ep * 1_000_000
        M = {"xu": r["x"] * 1000, "b": r["b"], "k": r["k"], "bs": r["bs"], "ks": r["ks"]}
        g = np.arange(5 * 1_000_000, (W - 4) * 1_000_000, STEP_US)
        t, tau = e_us + g, W - g / 1e6
        books = {n: (book_at(M, t + int((ad - s) * 1000)), book_at(M, t + int((ad + b + a.taker_delay_ms) * 1000)))
                 for n, ad, s, b in scen}  # fmt: skip
        used = False
        for s in (0, 1):
            L, T, P = B[s, i]
            if not len(L) or L[0] > e_us - VOL_WIN * 1_000_000:
                continue
            j0 = np.searchsorted(T, ep * 1000, "right") - 1
            vg = np.arange(e_us - VOL_WIN * 1_000_000, e_us, STEP_US)
            lr = np.diff(np.log(P[np.searchsorted(L, vg, "right") - 1]))
            sig = float(np.sqrt(np.sum(lr**2) / (len(lr) * STEP_US / 1e6)))
            if j0 < 0 or not sig > 0:
                continue
            used = True
            z = np.log(P[np.searchsorted(L, t, "right") - 1] / P[j0]) / (sig * np.sqrt(tau))
            fu = ndtr(a.k * z)
            ua, _, da, _ = books["tokyo"][0]
            for fr in ZFRAC:
                n = int(np.searchsorted(g, fr * W * 1e6))
                zrows.append((day, i, tf, ep, s, fr, float(z[n]), float((ua[n] + 1 - da[n]) / 2)))
            for name, (view, arr) in books.items():
                for th in THETAS:
                    best, anysig = None, False
                    for side, f, v, av, sz in (("UP", fu, view[0], arr[0], arr[1]),
                                               ("DOWN", 1 - fu, view[2], arr[2], arr[3])):  # fmt: skip
                        sg, n = first_fill(f, v, av, sz, th, a.fee_rate)
                        anysig |= sg
                        if n >= 0 and (best is None or n < best[0]):
                            best = (n, side, float(f[n]), float(av[n]), float(sz[n]))
                    if best:
                        orders.append((day, i, tf, ep, s, name, th, 1, best[1], g[best[0]] / 1e6, *best[2:]))
                    elif anysig:
                        orders.append((day, i, tf, ep, s, name, th, 0, "", np.nan, np.nan, np.nan, np.nan))
        nm += used
    pd.DataFrame(orders, columns=OCOLS).to_csv(a.res / f"orders-{day}.csv", index=False)
    pd.DataFrame(zrows, columns=ZCOLS).to_csv(a.res / f"z-{day}.csv", index=False)
    lat.update(markets=nm, pm_rows=int(len(pm)), bn_rows=int(len(bn)), taker_delay_ms=a.taker_delay_ms,
               proc_ms=a.proc_ms, k=a.k)  # fmt: skip
    (a.res / f"lat-{day}.json").write_text(json.dumps(lat))
    return lat


def cmd_eval(a):
    a.res.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    for d in a.days or sorted(p.name for p in a.rec.glob("????-??-??") if p.is_dir()):
        if (a.res / f"lat-{d}.json").exists() and d < today and not a.force:
            continue
        print(d, eval_day(a.rec, d, a), flush=True)


def settle(path, slugs, fetch=True):
    have = {}
    if path.exists():
        with path.open() as f:
            have = {r["slug"]: r["winner"] for r in csv.DictReader(f)}
    todo = [s for s in slugs if s not in have]
    for n in range(0, len(todo) if fetch else 0, 50):
        url = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(
            f"slug={s}" for s in todo[n : n + 50]
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "updown-shadow"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.load(r)
        except Exception as e:  # noqa: BLE001
            print("settle fetch failed", n, repr(e), flush=True)
            continue
        for m in rows:
            try:
                names = [x.upper() for x in json.loads(m["outcomes"])]
                pr = [float(v) for v in json.loads(m["outcomePrices"])]
            except (KeyError, TypeError, ValueError):
                continue
            if m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(pr) >= 0.99 and min(pr) <= 0.01:
                have[m["slug"]] = names[pr.index(max(pr))]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "winner"])
        w.writerows(sorted(have.items()))
    return have


def cmd_report(a):
    O = pd.concat([pd.read_csv(f) for f in sorted(a.res.glob("orders-*.csv"))], ignore_index=True)
    Z = pd.concat([pd.read_csv(f) for f in sorted(a.res.glob("z-*.csv"))], ignore_index=True)
    for D in (O, Z):
        D["asset"] = D.asset.map(dict(enumerate(rec.ASSETS)))
        D["src"] = D.src.map(dict(enumerate(SRC)))
        D["slug"] = D.asset + "-updown-" + D.tf.astype(str) + "m-" + D.epoch.astype(str)
    W = settle(a.res / "settlements.csv", sorted(set(Z.slug)), not a.no_fetch)
    O["w"], Z["w"] = O.slug.map(W), Z.slug.map(W)
    days = sorted(Z.day[Z.w.notna()].unique())
    nd = max(len(days), 1)
    print(f"days {days[:1]}..{days[-1:]} ({nd}); markets settled {Z[Z.w.notna()].slug.nunique()} of {Z.slug.nunique()}")
    for f in sorted(a.res.glob("lat-*.json")):
        print(f.stem, f.read_text())
    O = O[O.w.notna()]
    F = O[O.filled == 1].copy()
    F["pps"] = (F.side == F.w).astype(float) - F.px - fee(F.px, a.fee_rate)
    F["p5"], F["pc"] = SIZE * F.pps, np.minimum(F.asz, a.cap) * F.pps
    pd.set_option("display.width", 250)
    for s in SRC:
        H = F[(F.src == s) & (F.theta == a.theta)]
        if H.empty:
            print(f"\n== signal: Binance {s}: no fills at theta {a.theta}")
            continue
        scens = sorted(O.scen.unique(), key=lambda c: (c != "tokyo", c))

        def piv(v, fn):  # rows: timeframe x asset plus ALL; one column per scenario (never summed across)
            t = H.pivot_table(index=["tf", "asset"], columns="scen", values=v, aggfunc=fn)
            t.loc[("ALL", ""), :] = H.groupby("scen")[v].agg(fn)
            return t.reindex(columns=scens)

        print(f"\n== signal: Binance {s}, theta {a.theta}, first fill per market, held to settlement")
        print(f"$ per day at {SIZE:g} shares\n", (piv("p5", "sum") / nd).round(2).fillna(0).to_string())
        print("cents per share\n", (100 * piv("pps", "mean")).round(2).to_string())
        print("fills per day\n", (piv("pps", "count") / nd).round(1).fillna(0).to_string())
        print(f"$ per day taking the shown size, capped at {a.cap:g} shares\n", (piv("pc", "sum") / nd).round(2).fillna(0).to_string())
        print("median shares shown at fill\n", piv("asz", "median").round(0).to_string())
        fr = O[(O.src == s) & (O.theta == a.theta)].groupby("scen").filled.agg(["mean", "size"])
        print("share of signalled markets that fill\n", fr.round(3).to_string())
        D = H.pivot_table(index="day", columns="scen", values="p5", aggfunc="sum")
        D = D.reindex(index=days, columns=scens).fillna(0)
        for c in scens[1:]:
            m, lo, hi = boot((D[c] - D["tokyo"]).to_numpy())
            print(f"uplift {c} vs tokyo: {m:+.2f} $/day at {SIZE:g} shares" + (f" (90% CI {lo:+.2f} to {hi:+.2f})" if nd >= 5 else ""))
        T = F[F.src == s].pivot_table(index="theta", columns="scen", values="p5", aggfunc="sum") / nd
        print("$ per day by theta\n", T.reindex(columns=scens).round(2).fillna(0).to_string())
    ks = np.round(np.arange(0.3, 1.81, 0.05), 2)
    Zs = Z[Z.w.notna() & np.isfinite(Z.z)]
    rows = []
    for (s, tf, asset), g in Zs.groupby(["src", "tf", "asset"]):
        y = (g.w == "UP").to_numpy(float)
        rows.append((s, tf, asset, g.slug.nunique(), ks[np.argmax([loglik(ndtr(k * g.z.to_numpy()), y) for k in ks])]))
    print("\nbest model scale k (in sample; the run assumed the k in lat-*.json)\n",
          pd.DataFrame(rows, columns=["src", "tf", "asset", "markets", "k_best"]).to_string(index=False))  # fmt: skip


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("eval")
    p.add_argument("--rec", type=Path, required=True)
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--days", nargs="*")
    p.add_argument("--taker-delay-ms", type=float, default=50.0, help="exchange hold on marketable orders")
    p.add_argument("--proc-ms", type=float, default=10.0, help="signing + CLOB processing")
    p.add_argument("--relay-save-ms", type=float, nargs="*", default=[0.0, 10.0, 20.0])
    p.add_argument("--k", type=float, default=0.65)
    p.add_argument("--fee-rate", type=float, default=0.07)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--res", type=Path, required=True)
    p.add_argument("--theta", type=float, default=0.08)
    p.add_argument("--cap", type=float, default=100.0)
    p.add_argument("--fee-rate", type=float, default=0.07)
    p.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    {"eval": cmd_eval, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
