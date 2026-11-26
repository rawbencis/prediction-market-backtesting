"""Live probe vs paper trader, one UTC day: what the money did against what the paper model said it would do.

    python3 lp_arb/probe_vs_paper.py --day 2026-11-27 [--probe ~/lp-arb/probe] [--paper ~/lp-arb/paper]
                                     [--mode live|dry] [--json out.json]

Joins PROBE/DAY.jsonl with PAPER/DAY.jsonl by trigger id. The probe log's inherited paper-model records (`paper_hedge`,
`paper_settle`, `sim: true`; `hedge`/`settle` in logs written before 2026-11-26) are simulated and are excluded; their
count is reported. Paper's model comes only from PAPER/DAY.jsonl. The two processes run separate sockets, so ids (which carry
the trigger millisecond) rarely agree exactly; unmatched ids are then paired by (window, side, |dt| <= 250 ms),
nearest first, one-to-one, and the report says how many pairs came from each rule. Both sides are restricted to the
probe's uptime (probe_start .. probe_stop segments, 5 s margins); paper triggers outside it are only counted.

Reports:
  triggers      both (exact / by time) / probe-only / paper-only
  skips         probe `skip` records by reason
  lighter fill  share of probe IOCs that filled (live truth) vs paper's +500 ms "displayed" (d500) on the same
                triggers, and paper's d500 over all its triggers
  per pair      live c/share (`pair.pnl_per_share`) vs paper's modelled c/share on the same trigger;
                Lighter slippage = fill px vs the trigger's Lighter px (+ = worse: paid more on a buy, got less on a sell);
                PM slippage = hedge VWAP vs the trigger's hedge price and vs the ask when the first FAK was sent (+ = paid more);
                fees: modelled (`l_fill.fee_usd_model`, `pm_fill.fee_usd`) vs charged (`fees` records ~5 s after fills)
  unhedged      every `unhedged` / `unwind` with its realised cost, and `halt` records

Fees charged: Polymarket from each trade's `fee_rate_bps`, priced with the model's own form (shares * rate * p * (1 - p));
Lighter from the probe's `l_fee` records (taker_fee read as millionths of notional; inferred unit, see probe.py
lighter_fee_charged). The probe's P&L already uses the charged Lighter fee (`l_fee.realised_usd` = model - charged);
`live_cps` here is shown both at the model fee (the `pair` record) and at the charged fee.
"""

import argparse
import collections
import json
import statistics
from pathlib import Path

TOL_MS = 250


def load(p):
    out = []
    if p.exists():
        for line in p.open():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def tms(tid):
    return int(str(tid).rsplit("-", 1)[1])


def join(probe_trig, paper_trig):
    """-> {probe id: (paper id, 'exact'|'time')}, probe-only ids, paper-only ids."""
    pid = {r["id"] for r in paper_trig}
    m = {r["id"]: (r["id"], "exact") for r in probe_trig if r["id"] in pid}
    used = {v[0] for v in m.values()}
    cand = []
    for r in probe_trig:
        if r["id"] in m:
            continue
        for q in paper_trig:
            if q["id"] in used or q["window"] != r["window"] or q["side"] != r["side"]:
                continue
            dt = abs(tms(q["id"]) - tms(r["id"]))
            if dt <= TOL_MS:
                cand.append((dt, r["id"], q["id"]))
    for dt, a, b in sorted(cand):
        if a not in m and b not in used:
            m[a] = (b, "time")
            used.add(b)
    return m, [r["id"] for r in probe_trig if r["id"] not in m], [q["id"] for q in paper_trig if q["id"] not in used]


def uptime(probe, margin=5.0):
    """[(t0, t1)] while the probe was running: each probe_start to its probe_stop, else to the segment's last record
    (a day file that starts mid-run opens a segment at its first record)."""
    seg, cur, last = [], None, None
    for r in sorted(probe, key=lambda r: r.get("t_local", 0)):
        t = r.get("t_local")
        if t is None:
            continue
        if last is None and r["kind"] != "probe_start":
            cur = t  # day file starting inside a run (the run crossed 00:00 UTC)
        if r["kind"] == "probe_start":
            if cur is not None:
                seg.append((cur, last))
            cur = t
        elif r["kind"] == "probe_stop" and cur is not None:
            seg.append((cur, t))
            cur = None
        last = t
    if cur is not None:
        seg.append((cur, last))
    return [(a + margin, b - margin) for a, b in seg if b - a > 2 * margin]


def med(v):
    v = [x for x in v if x is not None]
    return round(statistics.median(v), 3) if v else None


SIM_KINDS = {"paper_hedge", "paper_settle", "hedge", "settle"}  # the probe log's inherited paper model (old logs: hedge/settle)


def is_sim(r):
    return bool(r.get("sim")) or r.get("kind") in SIM_KINDS


def summarise(probe, paper, mode="live"):
    sim = [r for r in probe if is_sim(r)]
    probe = [r for r in probe if not is_sim(r)]  # simulated paper records are never the probe's trades
    ofmode = [r for r in probe if r.get("mode") in (None, mode)]  # trigger/check have no mode; execution records do
    by = collections.defaultdict(list)
    for r in ofmode:
        by[r["kind"]].append(r)
    ptrig = {r["id"]: r for r in by["trigger"]}
    qrecs = collections.defaultdict(list)
    for r in paper:
        qrecs[r["kind"]].append(r)
    up = uptime(probe)
    inside = lambda t: any(a <= t <= b for a, b in up)  # noqa: E731
    qall = qrecs["trigger"]
    qtrig = {r["id"]: r for r in qall if inside(r["t_local"])}
    ptrig = {k: v for k, v in ptrig.items() if inside(v["t_local"])}
    qd500 = {r["id"]: bool(r["displayed"]) for r in qrecs["check"] if r.get("at_ms") == 500}
    qhedge = {r["id"]: r for r in qrecs["hedge"]}
    m, only_probe, only_paper = join(list(ptrig.values()), list(qtrig.values()))

    out = dict(mode=mode, sim_records_excluded=dict(collections.Counter(r["kind"] for r in sim)), uptime=dict(segments=len(up), minutes=round(sum(b - a for a, b in up) / 60, 1),
                                      paper_triggers_outside=len(qall) - len(qtrig)),
               triggers=dict(probe=len(ptrig), paper=len(qtrig), both=len(m),
                                        both_exact=sum(1 for v in m.values() if v[1] == "exact"),
                                        both_by_time=sum(1 for v in m.values() if v[1] == "time"),
                                        probe_only=len(only_probe), paper_only=len(only_paper)))
    out["skips"] = dict(collections.Counter(r["reason"] for r in by["skip"]).most_common())

    # ---- Lighter fill rate
    lord = [r for r in by["l_order"] if r.get("purpose") != "unwind"]
    sent = [r for r in lord if r.get("status") != "rejected"]
    filled = [r for r in sent if float(r.get("filled") or 0) > 0]
    conf = collections.Counter()
    for r in sent:
        q = m.get(r["id"])
        d = qd500.get(q[0]) if q else None
        conf[("filled" if float(r.get("filled") or 0) > 0 else "nofill", "paper_d500" if d else ("paper_not_d500" if d is False else "no_paper"))] += 1
    out["lighter_fill"] = dict(
        orders=len(lord), rejected=sum(1 for r in lord if r.get("status") == "rejected"),
        ambiguous=sum(1 for r in lord if r.get("status") == "ambiguous"), sent=len(sent), filled=len(filled),
        live_fill_rate=round(len(filled) / len(sent), 3) if sent else None,
        filled_share_of_size=round(sum(float(r.get("filled") or 0) for r in sent) / sum(float(r.get("size") or 0) for r in sent), 3) if sent else None,
        paper_d500_same_triggers=round(sum(1 for r in sent if qd500.get((m.get(r["id"]) or (None,))[0])) / len(sent), 3) if sent else None,
        paper_d500_all=round(sum(qd500.values()) / len(qd500), 3) if qd500 else None,
        live_vs_paper={f"{a}/{b}": n for (a, b), n in sorted(conf.items())},
        rejections=[dict(id=r["id"], reason=str(r.get("reason"))[:200]) for r in lord if r.get("status") == "rejected"][:10])

    # ---- per pair
    lfill = {r["id"]: r for r in by["l_fill"]}
    pmf = collections.defaultdict(list)
    for r in by["pm_fill"]:
        pmf[r["id"]].append(r)
    pmo = collections.defaultdict(list)
    for r in by["pm_order"]:
        pmo[r["id"]].append(r)
    fees = collections.defaultdict(lambda: {"lighter": [], "pm": []})
    for r in by["fees"]:
        if r.get("venue") == "pm":
            fees[r["id"]]["pm"].extend(r.get("trades") or [])
    lfee = collections.defaultdict(list)
    for r in by["l_fee"]:
        lfee[r["id"]].append(r)
    pairs = []
    for p in by["pair"]:
        tid, t = p["id"], ptrig.get(p["id"], {})
        side, lf = p["side"], lfill.get(p["id"], {})
        q = m.get(tid)
        qh = qhedge.get(q[0]) if q else None
        tl, th = t.get("lighter_px"), t.get("pm_px")
        lslip = None if tl is None else round(100 * ((p["lighter_px"] - tl) if side == "a" else (tl - p["lighter_px"])), 2)
        first_ask = next((o.get("ask") for o in sorted(pmo[tid], key=lambda o: o.get("attempt", 0)) if o.get("ask") is not None), None)
        entry_fee = [x for x in lfee[tid] if x.get("purpose") == "entry"]
        pm_ch = fees[tid]["pm"]
        l_ch = entry_fee[0].get("fee_charged_usd") if entry_fee else None
        l_adj = sum(x.get("realised_usd") or 0 for x in entry_fee)  # model - charged, for the whole Lighter fill
        fq = float(lf.get("qty") or p["qty"])
        pm_usd = sum(float(x["size"]) * float(x.get("fee_rate_bps") or 0) / 1e4 * float(x["price"]) * (1 - float(x["price"])) for x in pm_ch) if pm_ch else None
        pairs.append(dict(
            id=tid, paper_id=q[0] if q else None, join=q[1] if q else None, side=side, qty=p["qty"], attempts=p.get("attempts"),
            live_cps=round(100 * p["pnl_per_share"], 2),
            live_cps_charged_fee=round(100 * (p["pnl_usd"] + l_adj * p["qty"] / fq) / p["qty"], 2) if entry_fee else None,
            paper_cps=round(100 * qh["pnl_per_share"], 2) if qh and qh.get("hedged") else None,
            paper_hedged=bool(qh and qh.get("hedged")) if q else None, paper_reason=(qh or {}).get("reason"),
            trig_lighter_px=tl, lighter_px=p["lighter_px"], lighter_slip_c=lslip,
            trig_hedge_px=th, first_ask=first_ask, pm_px=p["pm_px"],
            pm_slip_vs_trigger_c=None if th is None else round(100 * (p["pm_px"] - th), 2),
            pm_slip_vs_ask_c=None if first_ask is None else round(100 * (p["pm_px"] - first_ask), 2),
            paper_pm_px=(qh or {}).get("pm_px") if qh and qh.get("hedged") else None,
            fee_model_lighter=lf.get("fee_usd_model"), fee_model_pm=round(sum(f.get("fee_usd") or 0 for f in pmf[tid]), 6),
            fee_charged_lighter_usd=l_ch, fee_lighter_applied=entry_fee[0].get("applied") if entry_fee else None,
            fee_lighter_rates_raw=sorted({t.get("fee_rate_raw") for t in entry_fee[0].get("trades") or ()}) if entry_fee else None,
            fee_charged_pm_usd=None if pm_usd is None else round(pm_usd, 6),
            fee_rate_bps_charged=sorted({x.get("fee_rate_bps") for x in pm_ch}) if pm_ch else None,
            fee_rate_model=(pmf[tid][0].get("fee_rate") if pmf[tid] else None), pnl_usd=p["pnl_usd"]))
    q = sum(x["qty"] for x in pairs)
    both = [x for x in pairs if x["paper_cps"] is not None]
    out["pairs"] = dict(
        n=len(pairs), shares=q, usd=round(sum(x["pnl_usd"] for x in pairs), 4),
        live_cps=round(100 * sum(x["pnl_usd"] for x in pairs) / q, 2) if q else None,
        with_paper_pair=len(both),
        live_cps_where_paper_paired=round(sum(x["live_cps"] * x["qty"] for x in both) / sum(x["qty"] for x in both), 2) if both else None,
        paper_cps_same=round(sum(x["paper_cps"] * x["qty"] for x in both) / sum(x["qty"] for x in both), 2) if both else None,
        paper_not_paired=sum(1 for x in pairs if x["paper_hedged"] is False),
        median_lighter_slip_c=med([x["lighter_slip_c"] for x in pairs]),
        median_pm_slip_vs_trigger_c=med([x["pm_slip_vs_trigger_c"] for x in pairs]),
        median_pm_slip_vs_ask_c=med([x["pm_slip_vs_ask_c"] for x in pairs]),
        fees_model_usd=dict(lighter=round(sum(x["fee_model_lighter"] or 0 for x in pairs), 6), pm=round(sum(x["fee_model_pm"] for x in pairs), 6)),
        fees_charged=dict(pm_usd=round(sum(x["fee_charged_pm_usd"] or 0 for x in pairs), 6),
                          pm_pairs_with_record=sum(1 for x in pairs if x["fee_charged_pm_usd"] is not None),
                          lighter_usd=round(sum(x["fee_charged_lighter_usd"] or 0 for x in pairs), 6),
                          lighter_pairs_with_record=sum(1 for x in pairs if x["fee_charged_lighter_usd"] is not None),
                          lighter_not_applied=sum(1 for x in pairs if x["fee_lighter_applied"] is False),
                          note="Lighter: taker_fee read as millionths of notional (inferred unit); the probe's P&L uses it"),
        live_cps_charged_fee=round(sum((x["live_cps_charged_fee"] if x["live_cps_charged_fee"] is not None else x["live_cps"]) * x["qty"]
                                       for x in pairs) / q, 2) if q else None,
        l_fee_true_up_usd=round(sum(r.get("realised_usd") or 0 for r in by["l_fee"]), 6),
        rows=pairs)

    # ---- unhedged / unwind / halts
    unw = {r["id"]: r for r in by["unwind"]}
    out["unhedged"] = dict(
        n=len(by["unhedged"]), loss_usd=round(sum(r.get("loss_usd") or 0 for r in by["unhedged"]), 4),
        residual_held_shares=round(sum(r.get("residual_held") or 0 for r in by["unhedged"]), 4),
        events=[dict(id=r["id"], side=r.get("side"), qty=r.get("qty"), unwound=r.get("unwound"), residual_held=r.get("residual_held"),
                     why_residual=r.get("why_residual"), loss_usd=r.get("loss_usd"),
                     unwind_px=(unw.get(r["id"]) or {}).get("px"), entry_px=(unw.get(r["id"]) or {}).get("entry_px"))
                for r in by["unhedged"]])
    out["unwinds"] = dict(n=len(by["unwind"]), realised_usd=round(sum(r.get("realised_usd") or 0 for r in by["unwind"]), 4))
    out["halts"] = [{k: r.get(k) for k in ("t_local", "reason", "id", "coid", "hedged")} for r in by["halt"]]
    out["realised_usd"] = round(sum(r.get("realised_usd") or 0 for k in ("pair", "unwind", "probe_settle", "l_fee") for r in by[k]), 4)
    out["only_probe_ids"], out["only_paper_ids"] = only_probe[:50], only_paper[:50]
    return out


def show(s):
    t = s["triggers"]
    print(f"uptime {s['uptime']}")
    print(f"mode {s['mode']}  triggers: probe {t['probe']}, paper {t['paper']}, both {t['both']} "
          f"(exact {t['both_exact']}, by time {t['both_by_time']}), probe-only {t['probe_only']}, paper-only {t['paper_only']}")
    print("skips:", s["skips"])
    lf = s["lighter_fill"]
    print(f"lighter IOC: {lf['sent']} sent ({lf['rejected']} rejected, {lf['ambiguous']} ambiguous), {lf['filled']} filled, "
          f"live fill rate {lf['live_fill_rate']} vs paper d500 on the same triggers {lf['paper_d500_same_triggers']} "
          f"(paper d500 all triggers {lf['paper_d500_all']}); {lf['live_vs_paper']}")
    p = s["pairs"]
    print(f"pairs: {p['n']}, {p['shares']} sh, ${p['usd']}, live {p['live_cps']} c/sh (model Lighter fee), "
          f"{p['live_cps_charged_fee']} c/sh (charged); where paper also paired ({p['with_paper_pair']}): "
          f"live {p['live_cps_where_paper_paired']} vs paper {p['paper_cps_same']} c/sh; paper did not pair {p['paper_not_paired']}")
    print(f"  median slippage c: lighter {p['median_lighter_slip_c']}, pm vs trigger {p['median_pm_slip_vs_trigger_c']}, "
          f"pm vs ask at send {p['median_pm_slip_vs_ask_c']}")
    print(f"  fees model {p['fees_model_usd']}, charged {p['fees_charged']}")
    for x in p["rows"]:
        print(f"   {x['id']} {x['side']} q={x['qty']} live {x['live_cps']}c paper {x['paper_cps']}c  L {x['trig_lighter_px']}->{x['lighter_px']} "
              f"({x['lighter_slip_c']}c)  PM {x['trig_hedge_px']}/{x['first_ask']}->{x['pm_px']} ({x['pm_slip_vs_trigger_c']}c)  att {x['attempts']}")
    u = s["unhedged"]
    print(f"unhedged: {u['n']}, loss ${u['loss_usd']}, residual held {u['residual_held_shares']} sh; unwinds {s['unwinds']}")
    for e in u["events"]:
        print("  ", e)
    print("halts:", s["halts"])
    print("realised_usd:", s["realised_usd"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--day", required=True)
    ap.add_argument("--probe", type=Path, default=Path.home() / "lp-arb" / "probe")
    ap.add_argument("--paper", type=Path, default=Path.home() / "lp-arb" / "paper")
    ap.add_argument("--mode", choices=("live", "dry"), default="live")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    pf, qf = a.probe.expanduser() / f"{a.day}.jsonl", a.paper.expanduser() / f"{a.day}.jsonl"
    for f in (pf, qf):
        if not f.exists():
            raise SystemExit(f"missing {f}")
    s = summarise(load(pf), load(qf), a.mode)
    if a.json:
        a.json.write_text(json.dumps(s, indent=1, default=str))
    show(s)
    return s


if __name__ == "__main__":
    main()
