"""Paper trader vs batch shadow, one UTC day: are the live decisions the ones the batch rule makes?

    python3 lp_arb/paper_vs_shadow.py --day 2026-11-25 [--paper ~/lp-arb/paper] [--shadow ~/lp-arb/shadow]

Batch side: the BTC trigger list in SHADOW/DAY.json (shadow_eval.py; 250 ms grid, recorder receive times).
Paper side: `trigger`, `check` (at_ms 500 = Lighter fill) and `hedge` lines of PAPER/DAY.jsonl (paper.py; every
book update, its own sockets). Triggers are matched by window and side, nearest in local time within +-1 s,
one-to-one, in time order. For unmatched triggers a reason is given:
  paper_down        no paper `health`/`trigger` line within 90 s (process not running)
  recorder_gap      the batch window had < 50% valid two-venue grid or no batch file
  near_threshold    edge within 0.5c of the 4c threshold (feeds 100+ ms apart can disagree)
  cooldown_chain    an earlier trigger on the same window+side within 5.5 s was matched with a different
                    fill outcome or unmatched, so the 500 ms / 5 s pauses diverged
  sub_grid          paper-only, condition visible to the paper process between 250 ms grid points
  other             none of the above
Writes PAPER/compare-DAY.json and prints a summary.
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAPER, SHADOW = Path.home() / "lp-arb" / "paper", Path.home() / "lp-arb" / "shadow"


def load_paper(p):
    trig, chk, hed, alive = {}, {}, {}, []
    if not p.exists():
        return trig, chk, hed, alive
    with p.open() as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            k = r.get("kind")
            if k in ("health", "trigger", "start", "window"):
                alive.append(r["t_local"])
            if k == "trigger":
                trig[r["id"]] = r
            elif k == "check":
                chk[(r["id"], r["at_ms"])] = r
            elif k == "hedge":
                hed[r["id"]] = r
    return trig, chk, hed, sorted(alive)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day")
    g.add_argument("--yesterday", action="store_true")
    ap.add_argument("--paper", type=Path, default=PAPER)
    ap.add_argument("--shadow", type=Path, default=SHADOW)
    a = ap.parse_args()
    if a.yesterday:
        a.day = str((datetime.now(timezone.utc) - timedelta(days=1)).date())
    trig, chk, hed, alive = load_paper(a.paper / f"{a.day}.jsonl")
    if not (a.paper / f"{a.day}.jsonl").exists():
        print(f"no paper log for {a.day}")
        return
    sp = a.shadow / f"{a.day}.json"
    doc = json.loads(sp.read_text())["assets"]["btc"] if sp.exists() else None
    batch = [dict(t, t=t["t_us"] / 1e6) for t in (doc["triggers"] if doc else [])]
    okwin = set()
    if doc:
        # windows with a usable batch book: per-window $ list is aligned with window_e; coverage is in the row only, so
        # treat windows that produced any batch trigger or appear in window_e as covered
        okwin = set(doc["window_e"])
    paper = [dict(r, t=r["t_local"], d500=bool(chk.get((r["id"], 500), {}).get("displayed")), e=r["window"]) for r in trig.values()]
    by = defaultdict(lambda: ([], []))
    for t in paper:
        by[(t["e"], t["side"])][0].append(t)
    for t in batch:
        by[(t["e"], t["side"])][1].append(t)
    matched, p_only, b_only = [], [], []
    for P, B in by.values():
        P.sort(key=lambda z: z["t"])
        B.sort(key=lambda z: z["t"])
        used = set()
        for p in P:
            best = None
            for j, b in enumerate(B):
                if j in used or abs(b["t"] - p["t"]) > 1.0:
                    continue
                if best is None or abs(b["t"] - p["t"]) < abs(B[best]["t"] - p["t"]):
                    best = j
            if best is None:
                p_only.append(p)
            else:
                used.add(best)
                p["match"] = B[best]
                B[best]["match"] = p
                matched.append((p, B[best]))
        b_only += [b for j, b in enumerate(B) if j not in used]

    def up(t):
        import bisect

        i = bisect.bisect_left(alive, t - 90)
        return i < len(alive) and alive[i] <= t + 90

    def reason(x, paper_side):
        if not up(x["t"]):
            return "paper_down"
        if x["e"] not in okwin:
            return "recorder_gap"
        if abs(x["edge"] - 0.04) < 0.005:
            return "near_threshold"
        same = (paper if paper_side else batch)
        for y in same:
            if y is not x and y["e"] == x["e"] and y["side"] == x["side"] and 0 < x["t"] - y["t"] <= 5.5:
                m = y.get("match")
                if m is None or bool(m.get("d500")) != bool(y.get("d500")):
                    return "cooldown_chain"
        return "sub_grid" if paper_side else "other"

    rp = Counter(reason(x, True) for x in p_only)
    rb = Counter(reason(x, False) for x in b_only)
    fill_agree = sum(bool(p["d500"]) == bool(b["d500"]) for p, b in matched)
    dt = sorted(p["t"] - b["t"] for p, b in matched)
    hed_n = sum(1 for p, _ in matched if hed.get(p["id"], {}).get("hedged"))
    paper_usd = sum(h.get("pnl_usd", 0) for h in hed.values() if h.get("hedged"))
    res = dict(day=a.day, paper_triggers=len(paper), batch_triggers=len(batch), matched=len(matched), paper_only=len(p_only),
               batch_only=len(b_only), paper_only_reasons=dict(rp), batch_only_reasons=dict(rb),
               fill_agreement_matched=(fill_agree / len(matched)) if matched else None,
               dt_paper_minus_batch_s=[dt[int(len(dt) * q)] for q in (0.1, 0.5, 0.9)] if dt else None,
               paper_hedged_pairs=sum(1 for h in hed.values() if h.get("hedged")), matched_hedged=hed_n,
               paper_usd=round(paper_usd, 2), batch_usd_500=round(sum(doc["per_window"]["usd"]), 2) if doc else None,
               batch_file=str(sp) if doc else None)
    (a.paper / f"compare-{a.day}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
