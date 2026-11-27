"""M2-0 tables: OhioAI's late (>= 240 s elapsed) BTC 5m buys.

  m0_report.py recorded   ~/m2-out/m0a/*.csv (from m0a_fills.py)       -> tables 1-4 (markdown on stdout)
  m0_report.py lifetime   ~/m2-out/ohioai/act.jsonl (Data API v2)       -> lifetime tables
  m0_report.py tcheck     Data API timestamp vs recorded match time (by tx hash), recorded days

Money: stake = price * size + taker fee (recorded: census fee; lifetime: usdc_size, which includes the taker fee),
payout = size if the side bought won. ROI = sum payout / sum stake - 1. Interval: 90% percentile bootstrap over windows
(4000 resamples, seed 7). "ex top 3" drops the 3 fills with the largest profit (payout - stake) in that subset.
Winners: ~/dublin-1124/settle.csv, then ~/m2-out/ohioai/winners.csv (Gamma via wallet_dissect.py settle).
"""
import csv
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

H = Path.home()
RNG = np.random.default_rng(7)
NB = 4000


def winners():
    w = {}
    for f in (H / "m2-out/ohioai/winners.csv", H / "dublin-1124/settle.csv"):
        if f.exists():
            for r in csv.DictReader(f.open()):
                w[r["slug"]] = r["winner"]
    return w


def roi_block(stake, pay, win_id, top_k=3):
    """returns (n, windows, staked, paid, roi, lo, hi) and the same ex top-k wins."""
    def one(s, p, g):
        n = len(s)
        if n == 0:
            return (0, 0, 0.0, 0.0, np.nan, np.nan, np.nan)
        ug, inv = np.unique(g, return_inverse=True)
        S, P = np.bincount(inv, s), np.bincount(inv, p)
        roi = P.sum() / S.sum() - 1
        k = len(ug)
        idx = RNG.integers(0, k, (NB, k))
        bs = P[idx].sum(1) / S[idx].sum(1) - 1
        lo, hi = np.percentile(bs, [5, 95])
        return (n, k, S.sum(), P.sum(), roi, lo, hi)
    stake, pay, win_id = map(np.asarray, (stake, pay, win_id))
    a = one(stake, pay, win_id)
    prof = pay - stake
    keep = np.ones(len(stake), bool)
    if len(stake) > top_k:
        keep[np.argsort(-prof)[:top_k]] = False
    b = one(stake[keep], pay[keep], win_id[keep])
    return a, b


def row(name, a, b):
    f = lambda r: (f"{r[0]} | {r[1]} | {r[2]:,.0f} | {r[3]:,.0f} | {100 * r[4]:+.1f}% | [{100 * r[5]:+.1f}, {100 * r[6]:+.1f}]"
                   if r[0] else "0 | 0 | | | |")
    return f"| {name} | {f(a)} | {100 * b[4]:+.1f}% [{100 * b[5]:+.1f}, {100 * b[6]:+.1f}] |" if b[0] else f"| {name} | {f(a)} | |"


HDR = ("| subset | fills | windows | staked $ | paid $ | ROI | 90% CI | ROI ex top-3 wins [90% CI] |\n"
       "|---|---|---|---|---|---|---|---|")
PB = ((0, 0.10, "price ≤ 10¢"), (0.10, 0.30, "10–30¢"), (0.30, 1.01, "> 30¢"))
SLB = ((40, 60.001, "60–40 s left"), (20, 40, "40–20 s left"), (5, 20, "20–5 s left"), (0, 5, "< 5 s left"))


def load_recorded():
    W = winners()
    rows = []
    for f in sorted(glob.glob(str(H / "m2-out/m0a/2026-11-*.csv"))):
        for r in csv.DictReader(open(f)):
            if not r.get("e"):
                continue
            rows.append(r)
    D = defaultdict(list)
    miss = 0
    for r in rows:
        w = W.get(r["slug"])
        if w is None:
            miss += 1
            continue
        for k in ("p", "sz", "fee", "left", "fv", "kgap", "age_ms", "rem500", "rem2000", "oth_sh", "lastgap_ms", "ask_1s"):
            D[k].append(float(r[k]) if r[k] not in ("", "nan") else np.nan)
        for k in ("e", "tok", "oth_n", "othb_n", "tjoin", "T"):
            D[k].append(int(r[k]))
        D["side"].append(r["side"])
        D["role"].append(r["role"])
        D["day"].append(r["day"])
        D["win"].append(float((w == "UP") == (int(r["tok"]) == 0)))
        D["tx"].append(r["tx"])
    D = {k: np.array(v) for k, v in D.items()}
    return D, len(rows), miss


def cmd_recorded():
    D, n_all, miss = load_recorded()
    print(f"Late fills read: {n_all}; without a winner: {miss}. Days: {', '.join(sorted(set(D['day'])))}.\n")
    sells = D["side"] == "SELL"
    print(f"SELL fills among them: {int(sells.sum())} (excluded below).\n")
    B = {k: v[~sells] for k, v in D.items()}
    B["stake"] = B["p"] * B["sz"] + B["fee"]
    B["pay"] = B["win"] * B["sz"]
    tail = B["p"] <= 0.30 + 1e-9
    print("### Table 1. Staked, paid out, ROI (recorded days)\n")
    print(HDR)
    subsets = [("all late buys", np.ones(len(B["p"]), bool)), ("  taker", B["role"] == "taker"), ("  maker", B["role"] == "maker")]
    subsets += [(nm, (B["p"] > lo + 1e-9 if lo else B["p"] >= 0) & (B["p"] <= hi + 1e-9)) for lo, hi, nm in PB]
    subsets += [(nm, (B["left"] > lo) & (B["left"] <= hi)) for lo, hi, nm in SLB]
    subsets += [("tail (≤ 30¢) " + nm, tail & (B["left"] > lo) & (B["left"] <= hi)) for lo, hi, nm in SLB]
    subsets += [("tail (≤ 30¢) all", tail), ("tail taker", tail & (B["role"] == "taker")), ("tail maker", tail & (B["role"] == "maker"))]
    for nm, m in subsets:
        a, b = roi_block(B["stake"][m], B["pay"][m], B["e"][m])
        print(row(nm, a, b))
    st = B["stake"][tail]
    print(f"\nTail stake per fill: median ${np.median(st):.2f}, p10–p90 ${np.percentile(st, 10):.2f}–${np.percentile(st, 90):.2f}; "
          f"tail stake per window (sum): median ${np.median(np.bincount(np.unique(B['e'][tail], return_inverse=True)[1], st)):.2f}.")
    big = np.argsort(-(B["pay"] - B["stake"])[tail])[:3]
    print("Three largest tail wins: " + "; ".join(
        f"{datetime.fromtimestamp(int(B['e'][tail][i]), timezone.utc):%m-%d %H:%M} {'Up' if B['tok'][tail][i] == 0 else 'Down'} "
        f"{100 * B['p'][tail][i]:.1f}¢ × {B['sz'][tail][i]:.0f} = +${(B['pay'] - B['stake'])[tail][i]:.0f}" for i in big) + ".\n")

    print("### Table 2. Calibration on tail buys (price ≤ 30¢), by FV − price\n")
    T = {k: v[tail] for k, v in B.items()}
    ok = np.isfinite(T["fv"])
    print(f"Tail buys {int(tail.sum())}, with FV {int(ok.sum())}.\n")
    print("| FV − price | fills | windows | mean price ¢ | mean FV ¢ | win rate ± SE | ROI [90% CI] | closer to win rate |\n|---|---|---|---|---|---|---|---|")
    g = 100 * (T["fv"] - T["p"])
    out2 = []
    for lo, hi, nm in ((-1e9, 0, "< 0"), (0, 5, "0–5¢"), (5, 15, "5–15¢"), (15, 1e9, "> 15¢")):
        m = ok & (g >= lo) & (g < hi)
        if not m.any():
            print(f"| {nm} | 0 | | | | | | |")
            out2.append((nm, None))
            continue
        wr, mp, mf = T["win"][m].mean(), T["p"][m].mean(), T["fv"][m].mean()
        se = np.sqrt(max(wr * (1 - wr), mp * (1 - mp)) / m.sum())
        a, _ = roi_block(T["stake"][m], T["pay"][m], T["e"][m])
        closer = "price" if abs(mp - wr) <= abs(mf - wr) else "FV"
        out2.append((nm, closer))
        print(f"| {nm} | {int(m.sum())} | {len(np.unique(T['e'][m]))} | {100 * mp:.1f} | {100 * mf:.1f} | {wr:.3f} ± {se:.3f} | "
              f"{100 * a[4]:+.0f}% [{100 * a[5]:+.0f}, {100 * a[6]:+.0f}] | {closer} (|p−w| {100 * abs(mp - wr):.1f}, |FV−w| {100 * abs(mf - wr):.1f}) |")
    m = ok
    print(f"\nAll tail buys with FV: mean price {100 * T['p'][m].mean():.1f}¢, mean FV {100 * T['fv'][m].mean():.1f}¢, "
          f"win rate {T['win'][m].mean():.3f}. Brier: price {np.mean((T['p'][m] - T['win'][m]) ** 2):.4f}, "
          f"FV {np.mean((T['fv'][m] - T['win'][m]) ** 2):.4f}.\n")
    json.dump({"table2_closer": out2}, open(H / "m2-out/table2.json", "w"))

    print("### Table 3. ROI by strike-gap tercile and by side\n")
    print("Strike gap = 1e4·(K − Chainlink at the start second)/Chainlink, signed so that positive favours the side bought.\n")
    for nm_set, m0 in (("all late buys", np.ones(len(B["p"]), bool)), ("tail buys (≤ 30¢)", tail)):
        kg = B["kgap"][m0]
        okk = np.isfinite(kg)
        q = np.percentile(kg[okk], [100 / 3, 200 / 3])
        print(f"**{nm_set}** (terciles at {q[0]:+.2f} / {q[1]:+.2f} bp)\n")
        print(HDR)
        idx = np.flatnonzero(m0)
        for nm, mm in ((f"gap < {q[0]:+.2f} bp", okk & (kg < q[0])), ("middle", okk & (kg >= q[0]) & (kg < q[1])),
                       (f"gap ≥ {q[1]:+.2f} bp", okk & (kg >= q[1])), ("Up", B["tok"][m0] == 0), ("Down", B["tok"][m0] == 1)):
            ii = idx[mm]
            a, b = roi_block(B["stake"][ii], B["pay"][ii], B["e"][ii])
            print(row(nm, a, b))
        print()

    print("### Table 4. Race on tail taker buys\n")
    tt = tail & (B["role"] == "taker")
    A = B["age_ms"][tt]
    oka = np.isfinite(A)
    print(f"Tail taker buys: {int(tt.sum())} (age known {int(oka.sum())}).\n")
    print("| measure | share | n |\n|---|---|---|")
    for nm, v in (("level had stood ≥ 500 ms before the match", A[oka] >= 500), ("level had stood ≥ 2 s", A[oka] >= 2000),
                  ("level appeared < 100 ms before the match", A[oka] < 100),
                  ("ask still ≤ fill price 500 ms after", B["rem500"][tt][np.isfinite(B["rem500"][tt])] > 0),
                  ("ask still ≤ fill price 2 s after", B["rem2000"][tt][np.isfinite(B["rem2000"][tt])] > 0),
                  ("another taker on the token within ±1 s", B["oth_n"][tt] > 0),
                  ("another taker BUY on the token within ±1 s", B["othb_n"][tt] > 0)):
        print(f"| {nm} | {v.mean():.2f} | {len(v)} |")
    lg, a1 = B["lastgap_ms"][tt], B["ask_1s"][tt]
    print(f"\nPrint stamp − last book row with ask ≤ fill price (ms), p10/p50/p90: "
          + " / ".join(f"{x:.0f}" for x in np.nanpercentile(lg, [10, 50, 90])) + f" (n {int(np.isfinite(lg).sum())}). "
          f"Side ask 1 s before the match ≤ the fill price: {np.mean(a1[np.isfinite(a1)] <= B['p'][tt][np.isfinite(a1)] + 1e-9):.2f}; "
          f"fill price − that ask, median {100 * np.nanmedian(B['p'][tt] - a1):+.1f}¢, p25/p75 "
          f"{100 * np.nanpercentile(B['p'][tt] - a1, 25):+.1f} / {100 * np.nanpercentile(B['p'][tt] - a1, 75):+.1f}¢.")
    print(f"\nLevel age, ms: p10/p50/p90 = {np.percentile(A[oka], 10):.0f} / {np.percentile(A[oka], 50):.0f} / {np.percentile(A[oka], 90):.0f}. "
          f"Other takers within ±1 s: mean {B['oth_n'][tt].mean():.1f} rows, {B['oth_sh'][tt].mean():.0f} shares.\n")
    print("ROI of tail taker buys by level age:\n")
    print(HDR)
    ii = np.flatnonzero(tt)
    for nm, mm in (("stood < 500 ms", oka & (A < 500)), ("stood 500 ms–2 s", oka & (A >= 500) & (A < 2000)), ("stood ≥ 2 s", oka & (A >= 2000)),
                   ("other taker within 1 s", B["oth_n"][tt] > 0), ("no other taker within 1 s", B["oth_n"][tt] == 0)):
        j = ii[mm]
        a, b = roi_block(B["stake"][j], B["pay"][j], B["e"][j])
        print(row(nm, a, b))


def load_lifetime():
    W = winners()
    out = defaultdict(list)
    nomatch = 0
    for l in open(H / "m2-out/ohioai/act.jsonl"):
        r = json.loads(l)
        s = r.get("slug") or ""
        if r.get("type") != "TRADE" or not s.startswith("btc-updown-5m-") or r.get("side") != "BUY":
            continue
        e = int(s.rsplit("-", 1)[1])
        t = int(r["timestamp"]) - 3
        el = t - e
        if el < 240 or el > 300:
            continue
        w = W.get(s)
        if w is None:
            nomatch += 1
            continue
        oi = r.get("outcomeIndex")
        o = (r.get("outcome") or "").upper()
        tok = 0 if o == "UP" else 1 if o == "DOWN" else int(oi)
        p, sz, usd = float(r["price"]), float(r["size"]), float(r.get("usdcSize") or 0)
        out["e"].append(e)
        out["p"].append(p)
        out["sz"].append(sz)
        out["stake"].append(usd if usd > 0 else p * sz)
        out["taker"].append(usd > p * sz * 1.0005 + 1e-6)
        out["pay"].append(sz * float((w == "UP") == (tok == 0)))
        out["left"].append(300 - el)
        out["mon"].append(datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m"))
        out["tx"].append(r.get("transactionHash"))
        out["ts"].append(int(r["timestamp"]))
    return {k: np.array(v) for k, v in out.items()}, nomatch


def cmd_lifetime():
    L, nm_ = load_lifetime()
    print(f"Lifetime late buys (block time − 3 s ≥ 240 s elapsed), BTC 5m, from 2026-08-14: {len(L['p'])} fills, "
          f"{len(np.unique(L['e']))} windows; without a winner: {nm_}.\n")
    tail = L["p"] <= 0.30 + 1e-9
    print(HDR)
    subs = [("all late buys", np.ones(len(L["p"]), bool))]
    subs += [(nm, (L["p"] > lo + 1e-9 if lo else L["p"] >= 0) & (L["p"] <= hi + 1e-9)) for lo, hi, nm in PB]
    subs += [("tail (≤ 30¢) all", tail), ("tail, fee-implied taker", tail & L["taker"]), ("tail, fee-implied maker", tail & ~L["taker"])]
    subs += [("tail " + nm, tail & (L["left"] > lo) & (L["left"] <= hi)) for lo, hi, nm in SLB]
    res = {}
    for nm, m in subs:
        a, b = roi_block(L["stake"][m], L["pay"][m], L["e"][m])
        res[nm] = (a, b)
        print(row(nm, a, b))
    print("\nBy month:\n")
    print(HDR)
    for mo in sorted(set(L["mon"])):
        for nm, m in (("all", L["mon"] == mo), ("tail ≤ 30¢", (L["mon"] == mo) & tail), ("≤ 10¢", (L["mon"] == mo) & (L["p"] <= 0.10 + 1e-9))):
            a, b = roi_block(L["stake"][m], L["pay"][m], L["e"][m])
            print(row(f"{mo} {nm}", a, b))
    print("\nTail stake by month (per fill and per window with a tail buy):\n")
    print("| month | tail fills | median $/fill | median $/window | p90 $/window |\n|---|---|---|---|---|")
    for mo in sorted(set(L["mon"])):
        m = (L["mon"] == mo) & tail
        if not m.any():
            continue
        pw = np.bincount(np.unique(L["e"][m], return_inverse=True)[1], L["stake"][m])
        print(f"| {mo} | {int(m.sum())} | {np.median(L['stake'][m]):.2f} | {np.median(pw):.2f} | {np.percentile(pw, 90):.2f} |")
    a, b = res["tail (≤ 30¢) all"]
    json.dump({"tail_ex3": {"roi": b[4], "lo": b[5], "hi": b[6]}, "tail": {"roi": a[4], "lo": a[5], "hi": a[6]}},
              open(H / "m2-out/lifetime_stop.json", "w"))
    print(f"\nStop-rule input: lifetime tail ex top-3 ROI {100 * b[4]:+.1f}%, 90% CI [{100 * b[5]:+.1f}, {100 * b[6]:+.1f}].")


def cmd_tcheck():
    D, _, _ = load_recorded()
    Tm = {tx: T for tx, T in zip(D["tx"], D["T"])}
    d = []
    for l in open(H / "m2-out/ohioai/act.jsonl"):
        r = json.loads(l)
        tx = (r.get("transactionHash") or "")[2:]
        if tx in Tm:
            d.append(int(r["timestamp"]) * 1000 - Tm[tx])
    d = np.array(d) / 1000
    print(f"Data API timestamp − recorded match time (s), {len(d)} late fills joined by tx: "
          f"p5/p25/p50/p75/p95 = " + " / ".join(f"{x:.2f}" for x in np.percentile(d, [5, 25, 50, 75, 95])))


if __name__ == "__main__":
    {"recorded": cmd_recorded, "lifetime": cmd_lifetime, "tcheck": cmd_tcheck}[sys.argv[1]]()
