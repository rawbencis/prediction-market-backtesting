"""Summarise a probe run and compare its triggers with the paper trader's over the same minutes.

    python3 lp_arb/probe_summary.py --probe ~/lp-arb/probe-dry --paper ~/lp-arb/paper [--md out.md]

The probe and lp-arb-paper run separate sockets, so the millisecond in a trigger id differs between them; triggers are
matched exactly by id and also by (window, side, |dt| <= 250 ms).
"""

import argparse
import collections
import json
from pathlib import Path

TOL_MS = 250


def load(root):
    out = []
    for f in sorted(Path(root).expanduser().glob("????-??-??.jsonl")):
        for line in f.open():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def tms(tid):
    return int(tid.rsplit("-", 1)[1])


def summarise(probe, paper):
    starts = [r["t_local"] for r in probe if r["kind"] == "probe_start"]
    stops = [r["t_local"] for r in probe if r["kind"] == "probe_stop"]
    t0, t1 = starts[0], (stops[-1] if stops else max(r["t_local"] for r in probe))
    pt = [r for r in probe if r["kind"] == "trigger"]
    qt = [r for r in paper if r["kind"] == "trigger" and t0 + 5 <= r["t_local"] <= t1 - 5]
    pt_in = [r for r in pt if t0 + 5 <= r["t_local"] <= t1 - 5]
    exact = {r["id"] for r in pt_in} & {r["id"] for r in qt}
    used, fuzzy, unmatched_probe = set(), [], []
    for r in pt_in:
        cand = [q for q in qt if q["id"] not in used and q["window"] == r["window"] and q["side"] == r["side"]
                and abs(tms(q["id"]) - tms(r["id"])) <= TOL_MS]
        if cand:
            q = min(cand, key=lambda q: abs(tms(q["id"]) - tms(r["id"])))
            used.add(q["id"])
            fuzzy.append((r, q))
        else:
            unmatched_probe.append(r)
    unmatched_paper = [q for q in qt if q["id"] not in used]
    k = collections.Counter(r["kind"] for r in probe)
    skips = collections.Counter(r["reason"] for r in probe if r["kind"] == "skip")
    lord = [r for r in probe if r["kind"] == "l_order" and r.get("purpose") != "unwind"]
    pairs = [r for r in probe if r["kind"] == "pair"]
    unh = [r for r in probe if r["kind"] == "unhedged"]
    pmo = [r for r in probe if r["kind"] == "pm_order"]
    settles = [r for r in probe if r["kind"] == "probe_settle"]
    paper_hedge = {r["id"]: r for r in paper if r["kind"] == "hedge"}
    disp500 = [r for r in probe if r["kind"] == "check" and r["at_ms"] == 500 and r["displayed"]]
    paper_disp = [r for r in paper if r["kind"] == "check" and r["at_ms"] == 500 and r["displayed"] and t0 + 5 <= r["t_local"] <= t1 - 5]
    clips = collections.Counter(r.get("sizing", {}).get("q") for r in lord)
    lf = [r for r in probe if r["kind"] == "l_fill"]
    per_pair = []
    for p in pairs:
        ph = paper_hedge.get(p["id"])
        per_pair.append(dict(id=p["id"], qty=p["qty"], lighter_px=p["lighter_px"], pm_px=p["pm_px"], cents=round(100 * p["pnl_per_share"], 2),
                             usd=p["pnl_usd"], attempts=p["attempts"], paper_model_cents=round(100 * ph["pnl_per_share"], 2) if ph and ph.get("hedged") else None))
    return dict(
        t0=t0, t1=t1, minutes=round((t1 - t0) / 60, 1), kinds=dict(k),
        probe_triggers=len(pt_in), paper_triggers=len(qt), exact_id_match=len(exact), matched_250ms=len(fuzzy),
        only_probe=[r["id"] for r in unmatched_probe], only_paper=[q["id"] for q in unmatched_paper],
        displayed_500=len(disp500), paper_displayed_500=len(paper_disp), skips=dict(skips), lighter_orders=len(lord),
        clips=dict(clips), lighter_fills=len(lf), lighter_filled_shares=sum(r["qty"] for r in lf),
        lighter_nofill=sum(1 for r in lord if r.get("status") == "nofill"), pm_orders=len(pmo),
        pm_attempt_status=dict(collections.Counter(r.get("status") for r in pmo)), pairs=len(pairs),
        pair_shares=sum(p["qty"] for p in pairs), pair_usd=round(sum(p["pnl_usd"] for p in pairs), 4),
        unhedged=len(unh), unhedged_loss_usd=round(sum(r.get("loss_usd") or 0 for r in unh), 4),
        settled_windows=len(settles), settled_pnl_usd=round(sum(s["pnl_usd"] for s in settles), 4),
        redeem_due=[r for r in probe if r["kind"] == "redeem_due"], per_pair=per_pair,
        errors=[r for r in probe if r["kind"] == "error"][:10], halts=[r for r in probe if r["kind"] == "halt"],
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", default="~/lp-arb/probe-dry")
    ap.add_argument("--paper", default="~/lp-arb/paper")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    s = summarise(load(a.probe), load(a.paper))
    if a.json:
        Path(a.json).write_text(json.dumps(s, indent=1, default=str))
    for k, v in s.items():
        if k not in ("per_pair", "errors", "redeem_due"):
            print(f"{k}: {v}")
    print("per pair:")
    for p in s["per_pair"]:
        print("  ", p)
    print("errors:", len(s["errors"]))
    for e in s["errors"]:
        print("  ", json.dumps(e)[:300])


if __name__ == "__main__":
    main()
