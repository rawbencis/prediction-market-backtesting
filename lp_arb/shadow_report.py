"""T1 paper shadow: cumulative table and the pass/kill check of frozen/t1_shadow_candidate.json,
with the definitions in frozen/t1_shadow_eval_spec.json.

    python lp_arb/shadow_report.py [--dir ~/lp-arb/shadow]

Reads DIR/days.csv and DIR/YYYY-MM-DD.json written by shadow_eval.py. Only BTC days on or after
2026-11-25 with counted=1 count, and only the first 14 of them. Writes DIR/report.md and DIR/status.json
and prints the report. Read-only otherwise.
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

OUT = Path.home() / "lp-arb" / "shadow"
START, NDAYS = "2026-11-25", 14
SPEC = json.loads((Path(__file__).resolve().parent.parent / "frozen" / "t1_shadow_candidate.json").read_text())


def boot(per_w, days, B=4000, seed=0):
    x = np.asarray(per_w, float)
    if len(x) < 5 or days <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    t = x[rng.integers(0, len(x), (B, len(x)))].sum(1) / days
    return float(np.percentile(t, 5)), float(np.percentile(t, 95))


def cps(fills):
    sh = sum(f["qty"] for f in fills)
    return 100 * sum(f["pnl"] * f["qty"] for f in fills) / sh if sh else float("nan")


def pess(f):
    return (f["d1"], f["d2"]) == (500, 500) and f["hedged"] and f.get("pess") and f.get("pess_h")


def f5(f):
    return (f["d1"], f["d2"]) == (500, 500) and f["hedged"]


def fmt(x, s="{:+.2f}"):
    return "n/a" if x is None or x != x else s.format(x)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dir", type=Path, default=OUT)
    a = ap.parse_args()
    p = a.dir / "days.csv"
    rows = list(csv.DictReader(p.open())) if p.exists() else []
    btc = [r for r in rows if r["asset"] == "btc"]
    counted = [r for r in btc if r["day"] >= START and r["counted"] == "1"][:NDAYS]
    docs = {r["day"]: json.loads((a.dir / f"{r['day']}.json").read_text())["assets"] for r in counted}
    L = []
    L.append(f"# T1 paper shadow (Lighter x Polymarket > 4c, BTC): report at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n")
    L.append(f"Rule: frozen/t1_lighter_pm_arb.json. Pass/kill: frozen/t1_shadow_candidate.json with frozen/t1_shadow_eval_spec.json. "
             f"Count starts {START} 00:00 UTC; {len(counted)} of {NDAYS} days counted.\n")
    L.append("## By day (all evaluated days; only `counted = 1` BTC days from 11-25 count)\n")
    L.append("| day | asset | counted | windows ok | triggers | fills | c/sh | $ | 1 s+1 s c/sh, $ | pessimistic fills, c/sh, $ | 5-sh clips $ | "
             "persist +300 (fills / triggers) | +100 / +500 (triggers) | raced | last 2 min $ | settle (n) | near 712772 | PM fee | "
             "PM disc rows, 1013 min, >1 s late, gaps > 5 s | Lighter unknown, gaps > 5 s |")
    L.append("|" + "---|" * 20)
    for r in rows:
        g = lambda k, s="{:+.2f}": fmt(float(r[k]) if r[k] not in ("", None) else None, s)  # noqa: E731
        L.append(f"| {r['day']} | {r['asset']} | {r['counted']} | {r['windows_ok']}/{r['windows']} | {r['triggers']} | {r['fills']} | {g('cps')} | "
                 f"{g('usd', '{:+.0f}')} | {g('cps_1s')}, {g('usd_1s', '{:+.0f}')} | {r['pess_fills']}, {g('pess_cps')}, {g('pess_usd', '{:+.0f}')} | "
                 f"{g('clip5_usd', '{:+.0f}')} | {g('persist300_fill', '{:.3f}')} / {g('persist300_trig', '{:.3f}')} | "
                 f"{g('persist100_trig', '{:.3f}')} / {g('persist500_trig', '{:.3f}')} | {g('raced_share', '{:.2f}')} | {g('late_usd_share', '{:.2f}')} | "
                 f"{g('settle_agree', '{:.3f}')} ({r['settle_n']}) | {g('near712772_share', '{:.2f}')} | {r['fee_rates']} | "
                 f"{r['pm_disc_rows']}, {r['pm_1013_min'] or 'n/a'}, {g('pm_late1s_share', '{:.4f}')}, {r['pm_gaps5s']} | {r['lt_unknown_rows']}, {r['lt_gaps5s']} |")
    st = dict(as_of=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), counted_days=[r["day"] for r in counted])
    if not counted:
        L.append("\n**No counted days yet.** Status: WAITING.\n")
        st["status"] = "WAITING"
    else:
        fills = [dict(f, day=d) for d in docs for f in docs[d]["btc"]["fills"]]
        P = [f for f in fills if pess(f)]
        per_w = [x for d in docs for x in docs[d]["btc"]["per_window"]["pess_usd"]]
        n = len(counted)
        lo, hi = boot(per_w, n)
        pday = {d: sum(f["pnl"] * f["qty"] for f in P if f["day"] == d) for d in docs}
        tot = sum(pday.values())
        F5 = [f for f in fills if f5(f)]
        pers = float(np.mean([f["disp300"] for f in F5])) if F5 else float("nan")
        trig = [t for d in docs for t in docs[d]["btc"]["triggers"]]
        pers_t = float(np.mean([t["d300"] for t in trig])) if trig else float("nan")
        disagree = [e for d in docs for e in docs[d]["btc"]["settle_disagree"]]
        sett_n = sum(int(r["settle_n"] or 0) for r in counted)
        ft = {d: tuple(map(tuple, docs[d]["btc"]["fee_types"])) for d in docs}
        rs = {d: tuple(docs[d]["btc"]["res_shas"]) for d in docs}
        first = sorted(docs)[0]
        fee_change = [d for d in docs if ft[d] != ft[first] or len(ft[d]) != 1]
        res_change = [d for d in docs if rs[d] != rs[first] or len(rs[d]) != 1]
        last7 = sorted(docs)[-7:]
        r7 = cps([f for f in P if f["day"] in last7]) if n >= 7 else float("nan")
        maxshare = max(pday.values()) / tot if tot > 0 else float("nan")
        c = cps(P)
        checks_pass = [
            ("BTC $/day 90% interval > 0 (pessimistic)", lo > 0, f"${tot / n:+.1f}/day [{lo:+.1f}, {hi:+.1f}]"),
            ("BTC c/share >= +2.0 (pessimistic)", c >= 2.0, f"{c:+.2f}c on {len(P)} fills"),
            ("Lighter quote displayed +300 ms >= 0.80 (fills)", pers >= 0.80, f"{pers:.3f} ({len(F5)} fills); per trigger {pers_t:.3f} ({len(trig)})"),
            ("settlement agreement 100%", not disagree and sett_n > 0, f"{sett_n - len(disagree)}/{sett_n}"),
            ("no day > 40% of the $", maxshare <= 0.40 if maxshare == maxshare else False, f"max day share {fmt(maxshare, '{:.2f}')}"),
        ]
        checks_kill = [
            ("rolling 7-day c/share < 0 (pessimistic)", n >= 7 and r7 < 0, fmt(r7) + ("" if n >= 7 else " (from day 7)")),
            ("+300 ms persistence < 0.65", pers < 0.65, f"{pers:.3f}"),
            ("any settlement disagreement", bool(disagree), f"{len(disagree)} windows {disagree[:5]}"),
            ("fee or resolution change", bool(fee_change or res_change), f"fee: {fee_change or 'none'}; resolution text: {res_change or 'none'}; "
                                                                         f"first-day fee {ft[first]}"),
        ]
        killed = [k for k, hit, _ in checks_kill if hit]
        if killed:
            status = "KILLED: " + "; ".join(killed)
        elif n < NDAYS:
            status = f"IN PROGRESS ({n}/{NDAYS} counted days)"
        else:
            status = "PASS" if all(ok for _, ok, _ in checks_pass) else "FAIL: " + "; ".join(k for k, ok, _ in checks_pass if not ok)
        L.append(f"\n## Cumulative, BTC, {n} counted days ({counted[0]['day']} .. {counted[-1]['day']})\n")
        allf = cps(F5)
        L.append(f"- all 500+500 fills: {len(F5)}, {allf:+.2f}c/share, ${sum(f['pnl'] * f['qty'] for f in F5) / n:+.1f}/day")
        L.append(f"- pessimistic: {len(P)} fills, {c:+.2f}c/share, ${tot / n:+.1f}/day [90% {lo:+.1f}, {hi:+.1f}]")
        L.append("\n| pass criterion (all needed at day 14) | now | met |\n|---|---|---|")
        L += [f"| {k} | {v} | {'yes' if ok else 'no'} |" for k, ok, v in checks_pass]
        L.append("\n| kill criterion (any) | now | hit |\n|---|---|---|")
        L += [f"| {k} | {v} | {'**YES**' if hit else 'no'} |" for k, hit, v in checks_kill]
        L.append("\nUnchecked: Lighter fee schedule changes (not in the recorded data). ETH is descriptive only (rows above).")
        L.append(f"\n**Status: {status}**\n")
        st.update(status=status, pess_usd_day=tot / n, pess_ci=[lo, hi], pess_cps=c, persist300_fill=pers, persist300_trig=pers_t,
                  settle=f"{sett_n - len(disagree)}/{sett_n}", rolling7_cps=r7, max_day_share=maxshare)
    txt = "\n".join(L) + "\n"
    a.dir.mkdir(parents=True, exist_ok=True)
    (a.dir / "report.md").write_text(txt)
    (a.dir / "status.json").write_text(json.dumps({k: (None if isinstance(v, float) and v != v else v) for k, v in st.items()}, indent=1))
    print(txt)


if __name__ == "__main__":
    main()
