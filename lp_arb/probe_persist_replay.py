"""Replay the +250 ms persistence check (probe.py) over one day of paper triggers, with recorder books.

    python3 lp_arb/probe_persist_replay.py [--paper lp_arb/tests/fixtures/paper_2026-11-25.jsonl] [--day 2026-11-25]
                                           [--shadow ~/lp-arb/shadow] [--json out.json]

The paper log has no Polymarket book after the trigger, so the books at trigger + 250 ms come from the recorders
(~/lighter-data lb, ~/shadow-rec pm; same box clock, separate sockets; read-only, as shadow_eval.py reads them). Each
trigger is fed to the probe's own `Probe.persist_check` (dry mode, temporary log dir, no venue calls). Triggers are
matched to the batch file's (SHADOW/DAY.json) BTC triggers with paper_vs_shadow.py's rule: same (window, side),
nearest in local time within 1.0 s, one-to-one, in time order. Hedged-pair P&L is the paper's own `hedge` record.
Also reports the recorder's edge at the trigger instant against the paper's logged edge (how far the two feeds agree).
"""

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lp_arb import paper as P  # noqa: E402
from lp_arb import probe as R  # noqa: E402
from lp_arb import shadow_eval as se  # noqa: E402  (pins the frozen recorder readers)

FIX = Path(__file__).resolve().parent / "tests" / "fixtures" / "paper_2026-11-25.jsonl"


def load_books(day):
    t0 = se.utc(day)
    keep = lambda x: (x["tf"] == 15) & (x["a"] == 0) & (x["e"] >= t0) & (x["e"] < t0 + 86400)  # noqa: E731  btc
    LB = np.sort(se.t1.load_hours(se.LT_DIR, "lb", se.le.LB_DT, day, keep), order="l")
    PM = np.sort(se.t1.load_hours(se.PM_DIR, "pm", se.le.PM_DT, day, keep), order="l")
    return {e: LB[LB["e"] == e] for e in np.unique(LB["e"])}, {e: PM[PM["e"] == e] for e in np.unique(PM["e"])}


def book_at(L, Pm, t_us, stamps=False):
    """(lb, la, lbs, las), (pb, pa, pbs, pas) as of local time t_us, or None where the recorder has no row yet.
    stamps=True: also (exchange ms, local s) of that row per venue."""
    out, st = [], []
    for X, f in ((L, ("b1", "k1", "b1s", "k1s")), (Pm, ("b", "k", "bs", "ks"))):
        i = se.le.asof(X["l"], t_us) if X is not None and len(X) else -1
        out.append(None if i < 0 else (X[f[0]][i] / 1e4, X[f[1]][i] / 1e4, float(X[f[2]][i]), float(X[f[3]][i])))
        st.append((0, 0.0) if i < 0 else (int(X["x"][i]), X["l"][i] / 1e6))
    return (out, st) if stamps else out


def paper_ages(r):
    """Book ages at the trigger from the paper record (the probe's book_ages at that instant, same code path):
    age = t_local - exchange ts; since_recv = book_age_ms; lag = age - since_recv."""
    out = {}
    for venue, xk, j in (("lighter", "t_exch_lighter", 0), ("pm", "t_exch_pm", 1)):
        x, since = r.get(xk), (r.get("book_age_ms") or [None, None])[j]
        if not x or since is None:
            out[venue] = dict(age_ms=None, lag_ms=None, since_recv_ms=None)
            continue
        age = r["t_local"] * 1000 - x
        out[venue] = dict(age_ms=round(age, 1), lag_ms=round(age - since, 1), since_recv_ms=since)
    return out


def set_book(book, q):
    book.clear()
    if q:
        bid, ask, bs, as_ = q
        if bid > 0:
            book.set(0, round(float(bid), 4), bs)
        if ask > 0:
            book.set(1, round(float(ask), 4), as_)


def match(paper, batch):
    by = defaultdict(lambda: ([], []))
    for t in paper:
        by[(t["window"], t["side"])][0].append(t)
    for b in batch:
        by[(b["e"], b["side"])][1].append(b)
    m = {}
    for Pt, B in by.values():
        Pt.sort(key=lambda z: z["t_local"])
        B.sort(key=lambda z: z["t_us"])
        used = set()
        for p in Pt:
            best = None
            for j, b in enumerate(B):
                dt = abs(b["t_us"] / 1e6 - p["t_local"])
                if j in used or dt > 1.0:
                    continue
                if best is None or dt < abs(B[best]["t_us"] / 1e6 - p["t_local"]):
                    best = j
            if best is not None:
                used.add(best)
                m[p["id"]] = B[best]
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--paper", type=Path, default=FIX)
    ap.add_argument("--day", default="2026-11-25")
    ap.add_argument("--shadow", type=Path, default=Path.home() / "lp-arb" / "shadow")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    recs = [json.loads(x) for x in a.paper.open()]
    trig = [r for r in recs if r["kind"] == "trigger"]
    wins = {r["e"]: r for r in recs if r["kind"] == "window"}
    d500 = {r["id"]: r["displayed"] for r in recs if r["kind"] == "check" and r["at_ms"] == 500}
    hedge = {r["id"]: r for r in recs if r["kind"] == "hedge" and r.get("hedged")}
    batch = json.loads((a.shadow / f"{a.day}.json").read_text())["assets"]["btc"]["triggers"]
    bm = match(trig, batch)
    LB, PM = load_books(day)

    tmp = Path(tempfile.mkdtemp(prefix="persist-replay-"))
    pr = R.Probe(R.ProbeLog(tmp, R.Redactor()), "dry", lighter=object(), pm=object(), stop_file=tmp / "STOP")
    rows, agree = [], []
    for r in trig:
        e, side = r["window"], r["side"]
        w = P.Window(e, wins[e]["lighter_market"] if e in wins else 0)
        w.up_token, w.pm_rate = "UP", r["pm_fee_rate"]
        L, Pm = LB.get(e), PM.get(e)
        t_us = int(r["t_local"] * 1e6)
        ql0, qp0 = book_at(L, Pm, t_us)  # recorder view at the trigger instant (calibration only)
        set_book(w.lbook, ql0), set_book(w.pbook, qp0)
        e0 = pr.pair_edge(w, side)[0]
        if e0 is not None:
            agree.append((r["edge"], e0))
        (ql, qp), ((lx, lt), (px_, pt)) = book_at(L, Pm, t_us + R.PERSIST_MS * 1000, stamps=True)
        set_book(w.lbook, ql), set_book(w.pbook, qp)
        w.lbook.x, w.lbook.t, w.pbook.x, w.pbook.t = lx, lt, px_, pt  # recorder row stamps at +250 ms
        a0 = paper_ages(r)
        pr.ages[r["id"]] = a0
        pr.clock = lambda t=r["t_local"] + R.PERSIST_MS / 1000: t
        pr.persist_check(w, r["id"], side)
        h = hedge.get(r["id"])
        rows.append(dict(id=r["id"], batch=r["id"] in bm, ok=pr.persist[r["id"]], trig_edge=r["edge"], d500=bool(d500.get(r["id"])),
                         hedged=h is not None, qty=h["qty"] if h else 0.0, pnl=h["pnl_per_share"] if h else 0.0))
    per = {p["id"]: p for p in (json.loads(x) for f in tmp.glob("*.jsonl") for x in f.open()) if p["kind"] == "persist"}
    for row in rows:
        pp = per[row["id"]]
        row["edge250"], row["edge_ok"] = pp["edge"], pp["ok"]
        row["stale0"] = R.Probe.stale_reason(pp["ages"]["trigger"])
        row["stale250"] = R.Probe.stale_reason(pp["ages"]["at_250"])
        row["ages"] = pp["ages"]

    def grp(sel):
        h = [x for x in sel if x["hedged"]]
        q = sum(x["qty"] for x in h)
        return dict(triggers=len(sel), displayed500=sum(x["d500"] for x in sel), pairs=len(h), shares=q,
                    usd=round(sum(x["qty"] * x["pnl"] for x in h), 2), cps=round(100 * sum(x["qty"] * x["pnl"] for x in h) / q, 2) if q else None)

    out = {}
    for name, f in (("all", lambda x: True), ("batch_matched", lambda x: x["batch"]), ("paper_only", lambda x: not x["batch"])):
        sel = [x for x in rows if f(x)]
        es = [x for x in sel if x["edge_ok"]]  # survive the 4c re-price (the 53 of the persistence addendum)
        out[name] = dict(all=grp(sel), edge_survive=grp(es),
                         edge_and_fresh_at_trigger=grp([x for x in es if not x["stale0"]]),
                         edge_and_fresh_both=grp([x for x in es if not x["stale0"] and not x["stale250"]]),
                         dropped_stale_at_trigger={k: sum(1 for x in es if x["stale0"] == k) for k in ("stale_pm", "stale_lighter")},
                         dropped_stale_at_250_only={k: sum(1 for x in es if not x["stale0"] and x["stale250"] == k)
                                                    for k in ("stale_pm", "stale_lighter")})

    def pct(v):
        v = [z for z in v if z is not None]
        return [round(float(np.percentile(v, q)), 1) for q in (10, 50, 90)] if v else None
    out["ages_p10_50_90_ms"] = {f"{when}.{venue}.{k}": pct([x["ages"][when][venue][k] for x in rows])
                                for when in ("trigger", "at_250") for venue in ("pm", "lighter") for k in ("age_ms", "lag_ms")}
    out["fresh_at_trigger_all_153"] = sum(1 for x in rows if not x["stale0"])
    dif = np.array([b - p for p, b in agree])
    out["recorder_vs_paper_edge_at_trigger"] = dict(n=len(agree), of=len(trig), median_c=round(100 * float(np.median(dif)), 2),
                                                    p10_c=round(100 * float(np.percentile(dif, 10)), 2),
                                                    p90_c=round(100 * float(np.percentile(dif, 90)), 2),
                                                    recorder_edge_ge_4c=int(sum(b >= 0.04 for _, b in agree)))
    out["no_book_at_250"] = sum(1 for x in rows if x["edge250"] is None)
    out["rows"] = rows
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))
    for k, v in out.items():
        if k != "rows":
            print(k, json.dumps(v))


if __name__ == "__main__":
    main()
