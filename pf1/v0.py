"""PF1 V0: replay the live Tokyo probe (11-26/27) on the recorded books and compare with what it really did.

For every live Lighter IOC the probe sent (status filled / nofill), start from the probe's own trigger (id, time, side,
Lighter price, Polymarket price) and simulate: persistence at +250 ms and the +500 ms display check (Tokyo views:
Lighter exchange + 2 ms, Polymarket exchange + 112 ms), Lighter IOC at +500 ms matched at send + 161 ms (measured
send -> exchange tx), Polymarket FAK at the Lighter fill (+330 ms known, +1.6 ms) arriving in London at send + 115 ms
and matched at arrival + 320 ms (live ack p50 ~435 ms - 115), with the competitor term (shares other takers took at our
limit or better between arrival and match are removed first; the probe's own fills are excluded), limit ask + 3c,
up to 4 attempts 420 ms apart, then unwind on Lighter. Latencies are fixed from measurement (amendment 2026-11-27),
not fitted. Pass: per day, on live-paired triggers that the simulator also pairs,
|sim - live| <= 1.5 c/share (pair P&L, share-weighted) and |median slippage sim - live| <= 2c.
Usage: python -I v0.py [--probe ~/lighter-import/probe-1126-1127.jsonl.gz] [--out out.json]
"""
import argparse
import collections
import gzip
import json
import statistics as st
import sys

import numpy as np
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine import Static  # noqa: E402
from sim import ProbeLat, iter_windows, probe_replay  # noqa: E402

# live targets (lp_arb/probe_vs_paper.py, share-weighted): all live pairs; pairs where paper also paired; median PM slippage
TARGET = {"2026-11-26": dict(all=-2.69, paper=-2.56, slip=9.0), "2026-11-27": dict(all=-0.93, paper=-1.35, slip=7.5)}


def load_probe(fp):
    by = collections.defaultdict(dict)
    pmo = collections.defaultdict(list)
    for ln in gzip.open(fp, "rt"):
        r = json.loads(ln)
        if "id" not in r:
            continue
        if r["kind"] == "pm_order":
            pmo[r["id"]].append(r)
        else:
            by[r["id"]].setdefault(r["kind"], r)
    return by, pmo


def mark_own(wins, pmo):
    """Mark the probe's own Polymarket fills in the Dublin trade feed (same token, taker BUY, price <= limit, exchange
    time within [send, ack + 100 ms]) so they are not counted as competitors. -> shares excluded."""
    n = 0.0
    for orders in pmo.values():
        for o in orders:
            w = wins.get(o.get("window"))
            if w is None or o.get("mode") != "live" or not o.get("shares"):
                continue
            T = w.pm.trades
            tk = 1 if o["side"] == "a" else 0
            left = float(o["shares"])
            for j in np.flatnonzero((T.x >= o["t_send"] * 1000) & (T.x <= o["t_ack"] * 1000 + 100) & (T.tok == tk) & (T.side == 0)
                                    & (T.p <= o["limit"] + 1e-9) & ~T.own):
                if left <= 1e-6:
                    break
                if T.sz[j] <= left + 1e-3:
                    T.own[j] = True
                    left -= T.sz[j]
                    n += T.sz[j]
    return n


def wmean(v, w):
    return sum(a * b for a, b in zip(v, w)) / sum(w) if v else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=Path, default=Path.home() / "lighter-import" / "probe-1126-1127.jsonl.gz")
    ap.add_argument("--census", default=str(Path(__file__).parent.parent / "reports" / "dublin" / "fees_by_window.csv"))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    by, pmo = load_probe(a.probe)
    st_ = Static(a.census)
    plat = ProbeLat()  # l_hold 161, pm_oneway 115, pm_hold 320, competitor term on
    comp = collections.defaultdict(dict)
    jobs = collections.defaultdict(list)
    for i, g in by.items():
        lo, tr = g.get("l_order"), g.get("trigger")
        if not lo or not tr or lo.get("mode") != "live" or lo.get("status") not in ("filled", "nofill"):
            continue
        day = datetime.fromtimestamp(tr["t_local"], timezone.utc).strftime("%Y-%m-%d")
        jobs[day].append((i, g))
    rows = []
    for day in sorted(jobs):
        eps = {g["trigger"]["window"] for _, g in jobs[day]}
        wins = {w.e: w for w in iter_windows(day, st_, only=eps)}
        comp[day]["own_trades_excluded"] = mark_own(wins, pmo)
        for i, g in jobs[day]:
            tr, lo = g["trigger"], g["l_order"]
            w = wins.get(tr["window"])
            if w is None:
                rows.append(dict(id=i, day=day, missing=True))
                continue
            q = float(lo["size"])
            s = probe_replay(w, tr["t_local"] * 1000, tr["side"], tr["lighter_px"], tr["pm_px"], q, plat, comp[day])
            live_pair = g.get("pair")
            row = dict(id=i, day=day, side=tr["side"], q=q, live_l_filled=lo["status"] == "filled", sim_l_filled=s["f_l"] > 0,
                       live_persist=g.get("persist", {}).get("ok"), sim_persist=s["persist"], sim_disp500=s["disp500"],
                       live_paired=bool(live_pair), sim_paired=s["f_pm"] > 0, sim_attempts=s["attempts"],
                       live_attempts=live_pair["attempts"] if live_pair else len(pmo.get(i, [])),
                       live_cps=100 * live_pair["pnl_per_share"] if live_pair else None, live_qty=live_pair["qty"] if live_pair else None,
                       live_slip=100 * (live_pair["pm_px"] - tr["pm_px"]) if live_pair else None,
                       sim_cps=s.get("pair_cps"), sim_slip=s.get("slip"), sim_unwound=s["unwound"], sim_unwind_usd=s["unwind_usd"],
                       live_unwind=bool(g.get("unwind")), live_unwind_usd=g.get("unwind", {}).get("realised_usd"),
                       paper_paired=bool(g.get("paper_hedge", {}).get("hedged")))
            rows.append(row)
    res = {}
    for day in sorted(jobs):
        R = [r for r in rows if r["day"] == day and not r.get("missing")]
        d = dict(l_orders=len(R), missing_window=sum(1 for r in rows if r["day"] == day and r.get("missing")),
                 lighter_fill_live=sum(r["live_l_filled"] for r in R), lighter_fill_sim=sum(r["sim_l_filled"] for r in R),
                 lighter_fill_agree=sum(r["live_l_filled"] == r["sim_l_filled"] for r in R),
                 persist_agree=f"{sum(r['live_persist'] == r['sim_persist'] for r in R)}/{len(R)}",
                 disp500_sim=sum(r["sim_disp500"] for r in R), sim_pairs_all=sum(r["sim_paired"] for r in R),
                 live_unwinds=sum(r["live_unwind"] for r in R), sim_unwinds=sum(r["sim_unwound"] > 0 for r in R))
        tg = TARGET.get(day, {})
        for name, sel in (("all", lambda r: r["live_paired"]), ("paper", lambda r: r["live_paired"] and r["paper_paired"])):
            lp = [r for r in R if sel(r)]
            both = [r for r in lp if r["sim_paired"]]
            x = dict(live_pairs=len(lp), live_shares=sum(r["live_qty"] for r in lp),
                     live_cps=wmean([r["live_cps"] for r in lp], [r["live_qty"] for r in lp]),
                     sim_paired_of_these=len(both),
                     live_cps_on_sim_paired=wmean([r["live_cps"] for r in both], [r["live_qty"] for r in both]),
                     sim_cps=wmean([r["sim_cps"] for r in both], [r["live_qty"] for r in both]),
                     live_slip_median=st.median(r["live_slip"] for r in lp) if lp else None,
                     sim_slip_median=st.median(r["sim_slip"] for r in both) if both else None,
                     live_attempts_mean=st.mean(r["live_attempts"] for r in lp) if lp else None,
                     sim_attempts_mean=st.mean(r["sim_attempts"] for r in both) if both else None,
                     target_cps=tg.get(name), target_slip=tg.get("slip"))
            x["pass_cps"] = bool(both) and abs(x["sim_cps"] - x["target_cps"]) <= 1.5
            x["pass_slip"] = bool(both) and abs(x["sim_slip_median"] - x["target_slip"]) <= 2.0
            d[name] = x
        d["competitor_term"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in comp[day].items()}
        res[day] = d
    print(json.dumps(res, indent=1, default=str))
    if a.out:
        a.out.write_text(json.dumps(dict(days=res, rows=rows), indent=1, default=str))


if __name__ == "__main__":
    main()
