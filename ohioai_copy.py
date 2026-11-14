"""Would copying OhioAI make money, and up to what delay? Read-only simulation on the shadow recorder.

For every OhioAI buy matched to a trade print (ohioai_match.py -> ohioai_fills_ms.csv), a copy order lands at
  their exchange match + detect delay + processing + taker hold + one-way to Polymarket
and takes the side's best ask standing at that exchange time, if it is <= their price + CAP and shows >= SIZE.
PnL per share = winner - ask - taker fee. Their own PnL on the same fills is the reference.

The detect delay is a grid; if the taker census (act_rec.py, act-*.bin) is recorded, the delay this box actually
saw OhioAI's trades on Polymarket's activity stream is measured and used as the 'measured' row.

  python3 ohioai_copy.py --rec ~/shadow-rec --fills ~/ohioai/ohioai_fills_ms.csv [--days D ...]
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import act_rec as ar
import shadow_eval as se
from leadlag import fee

OHIOAI = bytes.fromhex("129246beaac356e104c397f0985c11b7469be5a6")
DELAYS = (0, 100, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000)


def measured_delay(root, days, fills):
    """ms from OhioAI's exchange match to this box receiving it on the activity stream, per tx hash."""
    act = ar.read(root, days)
    if not len(act):
        return None
    act = act[act["w"] == OHIOAI]
    got = {}
    for t, lus in zip(act["tx"].tolist(), act["l"].tolist()):
        got.setdefault(t.ljust(32, b"\0").hex(), int(lus) // 1000)  # first sighting of each tx
    d = [got[h] - x for h, x in zip(fills.tx.fillna(""), fills.x_ms) if h in got]
    return np.asarray(d, float) if d else None


def books(root, days):
    pm = np.concatenate([se.read(root, d, "pm-", se.PM_DT) for d in days])
    out = {}
    for key in set(zip(pm["a"].tolist(), pm["tf"].tolist(), pm["e"].tolist())):
        m = (pm["a"] == key[0]) & (pm["tf"] == key[1]) & (pm["e"] == key[2])
        P = pm[m]
        out[key] = P[np.argsort(P["x"], kind="stable")]
    return out


def ask_at(P, t_ms, tok):
    """Best ask and its size for token tok (0 up, 1 down) standing at exchange time t_ms, else (nan, 0)."""
    i = np.searchsorted(P["x"], t_ms, "right") - 1
    if i < 0 or P["k"][i] <= 0:
        return np.nan, 0.0
    if tok == 0:
        return P["k"][i] / 1e4, float(P["ks"][i])
    return 1 - P["b"][i] / 1e4, float(P["bs"][i])


def simulate(F, B, delays, a):
    rows = []
    for r in F.itertuples():
        P = B.get((int(r.a_idx), int(r.tf), int(r.epoch)))
        if P is None:
            continue
        win = 1.0 if r.winner == ("UP" if r.side == 0 else "DOWN") else 0.0
        their = win - r.px - (fee(r.px, a.fee_rate) if r.taker else 0.0)
        for name, d in delays:
            land = r.x_ms + d + a.proc_ms + a.taker_delay_ms + a.order_ms
            ask, sz = ask_at(P, land, int(r.side))
            ok = bool(ask == ask and ask <= r.px + a.cap + 1e-9 and sz >= a.size)
            rows.append(dict(slug=r.slug, taker=int(r.taker), delay=name, their=their, filled=int(ok),
                             slip=(ask - r.px) if ok else np.nan,
                             net=(win - ask - fee(ask, a.fee_rate)) if ok else np.nan))  # fmt: skip
    return pd.DataFrame(rows)


def ci(v, g, n=1000, seed=7):
    """90% CI of the mean, bootstrapping whole markets (fills in one market are not independent)."""
    if len(v) < 5:
        return (np.nan, np.nan)
    df = pd.DataFrame({"v": v, "g": g})
    sums = df.groupby("g").v.agg(["sum", "count"])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(sums), (n, len(sums)))
    s, c = sums["sum"].to_numpy()[idx].sum(1), sums["count"].to_numpy()[idx].sum(1)
    return tuple(np.percentile(s / c, [5, 95]))


def report(S, out=print):
    for lab, part in (("taker fills", S[S.taker == 1]), ("maker fills", S[S.taker == 0])):
        if not len(part):
            continue
        base = part[part.delay == part.delay.iloc[0]]
        lo, hi = ci(100 * base.their.to_numpy(), base.slug.to_numpy())
        out(f"\n{lab}: {len(base)} of their buys; their own net {100 * base.their.mean():+.2f} c/share "
            f"[90% CI {lo:+.2f}, {hi:+.2f}]")  # fmt: skip
        out(f"  {'detect delay':>14} {'copied':>7} {'fill%':>6} {'paid vs them':>12} {'net c/share':>12} {'90% CI':>18}")
        for name, g in part.groupby("delay", sort=False):
            f = g[g.filled == 1]
            lo, hi = ci(100 * f.net.to_numpy(), f.slug.to_numpy())
            out(f"  {name:>14} {len(f):7d} {100 * g.filled.mean():6.1f} {100 * f.slip.mean():+11.2f}c "
                f"{100 * f.net.mean():+11.2f}  [{lo:+6.2f}, {hi:+6.2f}]")  # fmt: skip


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    ap.add_argument("--fills", type=Path, default=Path.home() / "ohioai" / "ohioai_fills_ms.csv")
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--cap", type=float, default=0.02, help="max price above theirs we accept")
    ap.add_argument("--size", type=float, default=5.0)
    ap.add_argument("--proc-ms", type=float, default=10.0)
    ap.add_argument("--taker-delay-ms", type=float, default=50.0)
    ap.add_argument("--order-ms", type=float, default=5.0, help="one-way to Polymarket: Dublin ~2-8, Tokyo ~120")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    F = pd.read_csv(a.fills)
    F = F[F.matched == 1].copy()
    F["day"] = pd.to_datetime(F.x_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    days = a.days or sorted(F.day.unique())
    F = F[F.day.isin(days)]
    F["a_idx"] = F.asset.map({s: i for i, s in enumerate(ar.ASSETS)})
    W = se.settle(a.fills.parent / "settlements.csv", sorted(F.slug.unique()), not a.no_fetch)
    F["winner"] = F.slug.map(W)
    n_all = len(F)
    F = F[F.winner.notna()]
    print(f"days {days}: {n_all} matched buys, {len(F)} in settled markets")
    delays = [(f"{d} ms", d) for d in DELAYS]
    D = measured_delay(a.rec, days, F) if "tx" in F else None
    if D is not None and len(D):
        q = np.percentile(D, [10, 50, 90])
        print(f"measured detect delay on this box (activity stream, n={len(D)}): p10/p50/p90 = "
              f"{q[0]:.0f}/{q[1]:.0f}/{q[2]:.0f} ms")  # fmt: skip
        delays.append((f"measured p50 {q[1]:.0f}", float(q[1])))
    else:
        print("no taker-census rows for these days yet: delay grid only")
    report(simulate(F, books(a.rec, days), delays, a))


if __name__ == "__main__":
    main()
