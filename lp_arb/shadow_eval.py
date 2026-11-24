"""T1 paper shadow, batch evaluator: the frozen T1 rule on one completed UTC day of fresh recordings.

    python -m lp_arb.shadow_eval --day 2026-11-25          # from the repo root
    python lp_arb/shadow_eval.py --day 2026-11-25          # same, as a script
    python lp_arb/shadow_eval.py --yesterday               # just yesterday (UTC)
    python lp_arb/shadow_eval.py --pending                 # what the daily timer runs: every completed UTC day from
                                                           # 2026-11-25 not yet in days.csv (oldest first, max 3 per run)

Rule: frozen/t1_lighter_pm_arb.json, unchanged. Measurement definitions: frozen/t1_shadow_eval_spec.json.
The trigger / fill / hedge logic is not re-implemented here: per window it calls the frozen
`lighter_eval.market_metrics` (vendored, byte-identical copy of ~/lighter-shadow @1348b2e, hash-checked
below) and `overnight/t1_arb.ext` (the event-time copy used for the holdout's descriptive rows). The
only new code is (a) loading, (b) fee lookup (lp_arb/fees.py), (c) the per-trigger persistence check,
which walks the same trigger sequence as ext() at 500+500 and asserts it gets the same trigger count,
and (d) recorder-health counters.

Inputs: ~/lighter-data (lighter_rec.py: lb/lt hourly files, lighter_markets.csv, lighter_outcomes.csv),
        ~/shadow-rec (shadow_rec.py: pm hourly files), Gamma (winners, fee schedule, resolution text),
        the updown-rec journal (1013 closes, drop counter). Read-only; no keys, no orders.
Outputs: OUT/YYYY-MM-DD.json (everything, incl. per-window $ and the trigger list) and one row per asset
         in OUT/days.csv (replaced if the day is re-run). OUT defaults to ~/lp-arb/shadow.
"""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
FROZEN_CODE = REPO / "lp_arb" / "frozen_code"
BLOBS = {"lighter_eval.py": "8d8571ea4b66fd1c4b6a028184838c131dc06247", "lighter_rec.py": "50fa2d65bcf12010d9a43468ce13bbf98900c594"}


def git_blob(p):
    b = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


for _f, _h in BLOBS.items():
    if git_blob(FROZEN_CODE / _f) != _h:
        sys.exit(f"{_f} differs from the frozen blob {_h}: refusing to run")
sys.path.insert(0, str(FROZEN_CODE))
import lighter_eval as le  # noqa: E402  (vendored frozen copy; must be imported before t1_arb)

sys.path.insert(0, str(REPO / "overnight"))
sys.path.insert(0, str(REPO))
import t1_arb as t1  # noqa: E402

from lp_arb import fees as lpfees  # noqa: E402

assert Path(le.__file__).resolve().parent == FROZEN_CODE, le.__file__

LT_DIR, PM_DIR = Path.home() / "lighter-data", Path.home() / "shadow-rec"
OUT = Path.home() / "lp-arb" / "shadow"
START_DAY = date(2026, 11, 25)  # count start (frozen/t1_shadow_eval_spec.json)
GAMMA = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&"
LCODE_UP = "2"  # Lighter outcome 2 = Up won, 1 = Down won (holdout 2,097/2,097)
COLS = ["day", "asset", "counted", "windows", "windows_ok", "triggers", "fills", "cps", "usd", "fills_1s", "cps_1s", "usd_1s",
        "pess_fills", "pess_cps", "pess_usd", "clip5_usd", "persist300_fill", "persist100_trig", "persist300_trig",
        "persist500_trig", "raced_share", "late_usd_share", "settle_agree", "settle_n", "near712772_share", "fee_rates",
        "fee_src", "pm_disc_rows", "pm_1013_min", "pm_drops_all", "pm_late1s_share", "pm_gaps5s", "lt_unknown_rows",
        "lt_gaps5s", "lt_late1s_share", "frozen_usd_grid", "eval_utc"]


def utc(d):
    return int(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def gamma_meta(day, assets=("btc", "eth")):
    """{(asset, e): dict(W, rate, ftype, res_sha)} for the day's 15m windows (closed markets)."""
    keys = [(a, e) for a in assets for e in range(utc(day), utc(day) + 86400, 900)]
    out = {}
    for i in range(0, len(keys), 40):
        slugs = {f"{a}-updown-15m-{e}": (a, e) for a, e in keys[i:i + 40]}
        url = GAMMA + "&".join(f"slug={s}" for s in slugs)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
                    rows = json.load(r)
                break
            except Exception as ex:  # noqa: BLE001
                print("gamma retry", ex, flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for m in rows:
            if m.get("slug") not in slugs:
                continue
            try:
                names = [n.upper() for n in json.loads(m["outcomes"])]
                pr = [float(v) for v in json.loads(m["outcomePrices"])]
                w = float(names[pr.index(max(pr))] == "UP") if max(pr) >= 0.99 else np.nan
            except (KeyError, ValueError, TypeError):
                w = np.nan
            fs = m.get("feeSchedule") or {}
            res = (m.get("resolutionSource") or "") + "\n" + (m.get("description") or "")
            out[slugs[m["slug"]]] = dict(W=w, rate=float(fs.get("rate", np.nan)), ftype=m.get("feeType") or "",
                                         res_sha=hashlib.sha1(res.encode()).hexdigest()[:12])
        time.sleep(0.6)
    return out


def triggers_500(L, P, e, fpm, tf=15):
    """Walk the same 500+500 trigger sequence as t1_arb.ext (same grid, trigger, pause and cooldown) and record,
    per trigger, whether the trigger-time Lighter quote (>= 5 sh at the trigger price or better) is still displayed
    100/300/500 ms later. +500 ms displayed == the Lighter leg fills (ext's fill condition)."""
    fl = le.fl
    g = np.arange(e * 1_000_000 + 5_000_000, (e + tf * 60) * 1_000_000, le.STEP)
    end = g[-1]

    def lbook(t):
        i = le.asof(L["l"], t)
        ii = np.maximum(i, 0)
        lb, la = L["b1"][ii] / 1e4, L["k1"][ii] / 1e4
        return lb, la, L["b1s"][ii], L["k1s"][ii], (i >= 0) & (lb > 0) & (la > lb)

    def pbook(t):
        j = le.asof(P["l"], t)
        jj = np.maximum(j, 0)
        pb, pa = P["b"][jj] / 1e4, P["k"][jj] / 1e4
        return pb, pa, P["bs"][jj], P["ks"][jj], (j >= 0) & (pb > 0) & (pa > pb)

    lb, la, lbs, las, vl = lbook(g)
    pb, pa, pbs, pas, vp = pbook(g)
    v = vl & vp
    ea = 1 - (la + (1 - pb) + fl(la) + fpm(1 - pb))
    eb = 1 - ((1 - lb) + pa + fl(lb) + fpm(pa))
    out = []
    D1 = 500_000
    for d, edge, lpx, lsz, hsz, ppx in (("a", ea, la, las, pbs, 1 - pb), ("b", eb, lb, lbs, pas, pa)):
        nxt = -1
        for i in np.flatnonzero(v & (edge > t1.TH) & (np.minimum(lsz, hsz) >= le.SIZE)):
            ti = g[i]
            if ti < nxt:
                continue
            tj, tk = ti + D1, ti + D1 + 500_000
            if tk > end:
                break
            nxt = tj
            rec = dict(t_us=int(ti), e=int(e), side=d, lpx=round(float(lpx[i]), 4), lsz=float(lsz[i]), pm_px=round(float(ppx[i]), 4),
                       pm_sz=float(hsz[i]), edge=round(float(edge[i]), 4))
            for chk in (100, 300, 500):
                yb, ya, ybs, yas, yv = (z[0] for z in lbook(np.array([ti + chk * 1000])))
                p_, s_ = (ya, yas) if d == "a" else (yb, ybs)
                rec[f"d{chk}"] = bool(yv and s_ >= le.SIZE and (p_ <= lpx[i] if d == "a" else p_ >= lpx[i]))
            if rec["d500"]:
                nxt = tj + le.COOL * le.STEP
            out.append(rec)
    return out


def health_pm(P, e, tf=15):
    if not len(P):
        return dict(rows=0, disc=0, late=0, gaps5=0)
    inwin = (P["l"] >= e * 1_000_000) & (P["l"] < (e + tf * 60) * 1_000_000)
    x = P[inwin]
    gaps = int((np.diff(x["l"]) > 5_000_000).sum()) if len(x) > 1 else 0
    has_x = x["x"] > 0
    late = int(((x["l"][has_x] / 1000 - x["x"][has_x]) > 1000).sum())
    return dict(rows=int(len(x)), disc=int(((x["b"] == 0) & (x["k"] == 0)).sum()), late=late, gaps5=gaps)


def health_lt(L, e, tf=15):
    if not len(L):
        return dict(rows=0, unknown=0, late=0, gaps5=0)
    inwin = (L["l"] >= e * 1_000_000) & (L["l"] < (e + tf * 60) * 1_000_000)
    x = L[inwin]
    gaps = int((np.diff(x["l"]) > 5_000_000).sum()) if len(x) > 1 else 0
    has_x = x["x"] > 0
    late = int(((x["l"][has_x] / 1000 - x["x"][has_x]) > 1000).sum())
    return dict(rows=int(len(x)), unknown=int(((x["b1"] == 0) & (x["k1"] == 0)).sum()), late=late, gaps5=gaps)


def journal(day):
    """updown-rec journal for the day: minutes whose last_err is a 1013 close, and the all-market drop counter delta."""
    since, until = f"{day} 00:00:00", f"{day + timedelta(days=1)} 00:00:00"
    try:
        txt = subprocess.run(["journalctl", "-u", "updown-rec", "--since", since, "--until", until, "--no-pager", "-o", "cat", "--utc"],
                             capture_output=True, text=True, timeout=120).stdout
    except Exception:  # noqa: BLE001
        return dict(m1013=None, drops=None)
    m1013 = sum(1 for ln in txt.splitlines() if "code=1013" in ln)
    drops = [int(ln.split("drops=")[1].split("/")[0]) for ln in txt.splitlines() if "drops=" in ln]
    d = (drops[-1] - drops[0]) if len(drops) >= 2 and drops[-1] >= drops[0] else None
    return dict(m1013=m1013 if txt else None, drops=d, lines=len(txt.splitlines()))


def evaluate(day, lt_dir=LT_DIR, pm_dir=PM_DIR, census_csv=lpfees.DEFAULT_CSV):
    t0 = utc(day)
    t1_ = t0 + 86400
    census = lpfees.load_census(census_csv)
    meta = gamma_meta(day)
    keep = lambda x: (x["tf"] == 15) & (x["a"] <= 1) & (x["e"] >= t0) & (x["e"] < t1_)  # noqa: E731
    LB = t1.load_hours(lt_dir, "lb", le.LB_DT, day, keep)
    LT = t1.load_hours(lt_dir, "lt", le.LT_DT, day, keep)
    if len(LT):
        _, first = np.unique(LT["id"], return_index=True)
        LT = LT[np.sort(first)]
    PM = t1.load_hours(pm_dir, "pm", le.PM_DT, day, keep)
    with (lt_dir / "lighter_markets.csv").open() as f:
        mk = {int(r[3]): (r[0], int(r[2])) for r in csv.reader(f) if len(r) >= 4}
    with (lt_dir / "lighter_outcomes.csv").open() as f:
        lo = {mk[int(r[0])]: r[2] for r in csv.reader(f) if len(r) >= 3 and int(r[0]) in mk}
    res = []
    for ai in (0, 1):
        nm = le.lr.ASSETS[ai]
        for e in range(t0, t1_, 900):
            L = np.sort(LB[(LB["a"] == ai) & (LB["e"] == e)], order="l")
            T = np.sort(LT[(LT["a"] == ai) & (LT["e"] == e)], order="l")
            P = np.sort(PM[(PM["a"] == ai) & (PM["e"] == e)], order="l")
            m = meta.get((nm, e), dict(W=np.nan, rate=np.nan, ftype="", res_sha=""))
            c, fsrc = lpfees.rate_for(nm, e, census, m["rate"])
            le.fp = lambda p, c=c: c * p * (1 - p)  # noqa: E731  (market_metrics reads the module global, as in t1_arb)
            w = dict(asset=nm, e=e, W=m["W"], lcode=lo.get((nm, e)), fee=c, fsrc=fsrc, ftype=m["ftype"], gamma_rate=m["rate"],
                     res_sha=m["res_sha"], nl=int(len(L)), npm=int(len(P)), hpm=health_pm(P, e), hlt=health_lt(L, e))
            if len(L) == 0:
                w.update(r={}, fills=[], trig={}, trigs=[])
                res.append(w)
                continue
            r = le.market_metrics(L, T, P, w["W"], e)
            r.pop("spreads"), r.pop("sizes"), r.pop("trades")
            w["r"] = {k: float(v) for k, v in r.items() if k.startswith(("seq0.04", "both_valid", "grid", "l_valid"))}
            fills, trig = t1.ext(L, T, P, w["W"], e, le.fp) if len(P) else ([], {})
            w["fills"], w["trig"] = fills, {f"{k[0]}+{k[1]}": v for k, v in trig.items()}
            w["trigs"] = triggers_500(L, P, e, le.fp) if len(P) else []
            assert len(w["trigs"]) == trig.get((500, 500), 0), (nm, e, len(w["trigs"]), trig.get((500, 500)))
            res.append(w)
    return res


def summarise(day, res, jr):
    rows, detail = [], {}
    for nm in ("btc", "eth"):
        ws = [w for w in res if w["asset"] == nm]
        ok = [w for w in ws if w.get("r") and w["r"].get("grid") and w["r"].get("both_valid", 0) / w["r"]["grid"] >= 0.5]
        f5 = [f for w in ws for f in w["fills"] if (f["d1"], f["d2"]) == (500, 500) and f["hedged"]]
        f1 = [f for w in ws for f in w["fills"] if (f["d1"], f["d2"]) == (1000, 1000) and f["hedged"]]
        pess = [f for f in f5 if f.get("pess") and f.get("pess_h")]
        trs = [t for w in ws for t in w["trigs"]]
        usd = lambda L: sum(f["pnl"] * f["qty"] for f in L)  # noqa: E731
        cps = lambda L: 100 * usd(L) / sum(f["qty"] for f in L) if L else float("nan")  # noqa: E731
        mean = lambda L: float(np.mean(L)) if len(L) else float("nan")  # noqa: E731
        sett = [(w["lcode"] == LCODE_UP) == (w["W"] == 1.0) for w in ws if w["lcode"] in ("1", "2") and w["W"] == w["W"]]
        near = [f["maker712772"] for f in f5 if "maker712772" in f]
        late_usd = usd([f for f in f5 if f["t_rel"] >= 780])
        hp = [w["hpm"] for w in ws]
        hl = [w["hlt"] for w in ws]
        per_w = {k: [] for k in ("usd", "pess_usd", "usd_1s", "frozen_grid_usd")}
        for w in ws:
            fs = [f for f in w["fills"] if f["hedged"]]
            per_w["usd"].append(usd([f for f in fs if (f["d1"], f["d2"]) == (500, 500)]))
            per_w["pess_usd"].append(usd([f for f in fs if (f["d1"], f["d2"]) == (500, 500) and f.get("pess") and f.get("pess_h")]))
            per_w["usd_1s"].append(usd([f for f in fs if (f["d1"], f["d2"]) == (1000, 1000)]))
            per_w["frozen_grid_usd"].append(w.get("r", {}).get("seq0.04_usd", 0.0))
        npm_rows = sum(h["rows"] for h in hp)
        nlt_rows = sum(h["rows"] for h in hl)
        row = dict(day=str(day), asset=nm, counted=int(nm == "btc" and len(ok) >= 0.9 * 96 and day >= START_DAY),
                   windows=len(ws), windows_ok=len(ok), triggers=len(trs), fills=len(f5), cps=cps(f5), usd=usd(f5),
                   fills_1s=len(f1), cps_1s=cps(f1), usd_1s=usd(f1), pess_fills=len(pess), pess_cps=cps(pess), pess_usd=usd(pess),
                   clip5_usd=sum(f["pnl"] * 5 for f in f5), persist300_fill=mean([f["disp300"] for f in f5]),
                   persist100_trig=mean([t["d100"] for t in trs]), persist300_trig=mean([t["d300"] for t in trs]),
                   persist500_trig=mean([t["d500"] for t in trs]), raced_share=mean([f["raced"] for f in f5]),
                   late_usd_share=late_usd / usd(f5) if f5 and usd(f5) else float("nan"),
                   settle_agree=mean(sett), settle_n=len(sett), near712772_share=mean(near),
                   fee_rates=";".join(f"{k}:{v}" for k, v in sorted(Counter(w["fee"] for w in ws).items())),
                   fee_src=";".join(f"{k}:{v}" for k, v in sorted(Counter(w["fsrc"] for w in ws).items())),
                   pm_disc_rows=sum(h["disc"] for h in hp), pm_1013_min=jr.get("m1013"), pm_drops_all=jr.get("drops"),
                   pm_late1s_share=sum(h["late"] for h in hp) / npm_rows if npm_rows else float("nan"),
                   pm_gaps5s=sum(h["gaps5"] for h in hp), lt_unknown_rows=sum(h["unknown"] for h in hl),
                   lt_gaps5s=sum(h["gaps5"] for h in hl), lt_late1s_share=sum(h["late"] for h in hl) / nlt_rows if nlt_rows else float("nan"),
                   frozen_usd_grid=sum(per_w["frozen_grid_usd"]), eval_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        rows.append(row)
        detail[nm] = dict(row=row, per_window=per_w, window_e=[w["e"] for w in ws],
                          fee_types=sorted({(w["ftype"], w["gamma_rate"]) for w in ws if w["ftype"]}, key=str),
                          res_shas=sorted({w["res_sha"] for w in ws if w["res_sha"]}),
                          settle_disagree=[w["e"] for w in ws if w["lcode"] in ("1", "2") and w["W"] == w["W"]
                                           and (w["lcode"] == LCODE_UP) != (w["W"] == 1.0)],
                          triggers=[t for w in ws for t in w["trigs"]],
                          fills=[dict(f, e=w["e"]) for w in ws for f in w["fills"] if (f["d1"], f["d2"]) in ((500, 500), (1000, 1000))])
    return rows, detail


def write(day, rows, detail, jr, out=OUT):
    out.mkdir(parents=True, exist_ok=True)

    def clean(o):
        if isinstance(o, float) and o != o:
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, np.integer, np.bool_)):
            return clean(o.item())
        return o

    doc = dict(day=str(day), spec="frozen/t1_shadow_eval_spec.json", rule="frozen/t1_lighter_pm_arb.json",
               code_blobs=BLOBS, journal=jr, assets=detail)
    tmp = out / f".{day}.json.tmp"
    tmp.write_text(json.dumps(clean(doc), indent=1))
    tmp.replace(out / f"{day}.json")
    p = out / "days.csv"
    old = []
    if p.exists():
        with p.open() as f:
            old = [r for r in csv.DictReader(f) if r["day"] != str(day)]
    new = old + [{k: ("" if v is None or (isinstance(v, float) and v != v) else v) for k, v in r.items()} for r in rows]
    new.sort(key=lambda r: (r["day"], r["asset"]))
    tmp = out / ".days.csv.tmp"
    with tmp.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(new)
    tmp.replace(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day")
    g.add_argument("--yesterday", action="store_true")
    g.add_argument("--pending", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--lt", type=Path, default=LT_DIR)
    ap.add_argument("--pm", type=Path, default=PM_DIR)
    ap.add_argument("--fee-csv", type=Path, default=lpfees.DEFAULT_CSV)
    a = ap.parse_args()
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    if a.pending:
        have = set()
        if (a.out / "days.csv").exists():
            with (a.out / "days.csv").open() as f:
                have = {r["day"] for r in csv.DictReader(f) if r["asset"] == "btc"}
        days, d = [], START_DAY
        while d <= yday:
            if str(d) not in have:
                days.append(d)
            d += timedelta(days=1)
        days = days[:3]
        print(f"pending days: {[str(x) for x in days]}", flush=True)
    else:
        days = [yday if a.yesterday else date.fromisoformat(a.day)]
    for day in days:
        run_day(day, a)


def run_day(day, a):
    t_start = time.time()
    res = evaluate(day, a.lt, a.pm, a.fee_csv)
    jr = journal(day)
    rows, detail = summarise(day, res, jr)
    write(day, rows, detail, jr, a.out)
    for r in rows:
        print(f"{r['day']} {r['asset']}: counted={r['counted']} windows {r['windows_ok']}/{r['windows']}, triggers {r['triggers']}, "
              f"fills {r['fills']}, {r['cps']:+.2f}c/sh, ${r['usd']:+.1f}; 1s+1s {r['cps_1s']:+.2f}c ${r['usd_1s']:+.1f}; "
              f"pessimistic {r['pess_fills']} {r['pess_cps']:+.2f}c ${r['pess_usd']:+.1f}; clip5 ${r['clip5_usd']:+.1f}; "
              f"persist300 fill {r['persist300_fill']:.3f} / trig {r['persist300_trig']:.3f}; settle {r['settle_agree']:.3f} (n {r['settle_n']}); "
              f"fees {r['fee_rates']} [{r['fee_src']}]", flush=True)
    print(f"done in {time.time() - t_start:.0f} s -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
