"""R-D3 evaluator (registered test, rule text in frozen/R-D3.md). One recorded day -> ~/rd3-res/rd3-<day>.csv, one row per
signal x signal source (fut primary, spot extra) x scenario (dublin primary, tokyo, colo extras), filled or not.
BLIND: prints only the row count. Never print, summarise or read the results.
Usage: python rd3_eval.py --day 2026-11-15
"""
import argparse, csv, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d

OUTD = Path.home() / "rd3-res"
FIRST_DAY = "2026-11-15"
SRC = {"fut": 1, "spot": 0}
M_BP, LOOK_MS, DEBOUNCE_MS = 2.0, 500, 2000
LEFT_MIN, LEFT_MAX, ASK_MIN, ASK_MAX = 30, 300, 1000, 9000
SIZE, CAP, HOLD, DECIDE_MS, SEND_DEFAULT = 5.0, 100, 50, 1, 22.0
TOKYO = dict(bn=2.0, book=116.0, send=120.0)
COLO = dict(book=1.0, send=1.0)


def signals(bn, src):
    """BTC Binance trades of src: (trade ms T, local receipt ms, side +1/-1) where the trade completes a >= 2 bp move vs
    the last price at or before T - 500 ms; 2 s debounce."""
    b = bn[(bn["src"] == src) & (bn["a"] == 0)]
    b = b[np.argsort(b["T"], kind="stable")]
    T, p, L = b["T"].astype(np.int64), b["p"], b["l"] / 1000.0
    j = np.searchsorted(T, T - LOOK_MS, "right") - 1
    ok = j >= 0
    r = np.where(ok, (p / p[np.maximum(j, 0)] - 1) * 1e4, 0.0)
    out, last = [], -10**15
    for i in np.flatnonzero(np.abs(r) >= M_BP):
        if T[i] - last >= DEBOUNCE_MS:
            out.append((int(T[i]), float(L[i]), 1 if r[i] > 0 else -1))
            last = T[i]
    return out


def seen_local(m):
    """-> f(t_local_ms) = index of the newest-by-exchange-time book row received on this box by t (or -1)."""
    B = m["book"]
    o = np.argsort(B["l"], kind="stable")
    lsorted = B["l"][o] / 1000.0
    best = np.maximum.accumulate(o)  # rows are in exchange-time order, so the max index = newest exchange time
    return lambda t: int(best[k]) if (k := int(np.searchsorted(lsorted, t, "right")) - 1) >= 0 else -1


def seen_ask(B, i, tok):
    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):
        return None
    a = int(B["k"][i]) if tok == 0 else (10000 - int(B["b"][i]) if B["b"][i] else 0)
    return a if a else None


def send_p50(day, path=Path.home() / "order-probe.csv"):
    """Day's median send -> exchange-visible (ms) from the order probe; 22 ms if unmeasured."""
    try:
        v = [float(r["send_to_ex_visible"]) for r in csv.DictReader(open(path))
             if r.get("send_to_ex_visible") not in ("", None) and time.strftime("%Y-%m-%d", time.gmtime(int(r["t_send"]) / 1000)) == day]
    except (OSError, KeyError, ValueError):
        v = []
    v = [x for x in v if 0 < x < 2000]  # <= 0: probe detector caught another trader's order
    return float(np.median(v)) if v else SEND_DEFAULT


def evaluate(mk, sigs, src, winners, send_ms):
    rows = []
    seen_fns = {}
    for T, lrx, sgn in sigs:
        tok = 0 if sgn > 0 else 1
        for scn in ("dublin", "tokyo", "colo"):
            if scn == "tokyo":
                dec = T + TOKYO["bn"] + DECIDE_MS
            else:  # dublin and colo (London) receive Binance as this box did
                dec = lrx + DECIDE_MS
            e = int(dec // 1000) // 300 * 300
            left = e + 300 - dec / 1000
            m = mk.get(("btc", 5, e))
            if m is None or not LEFT_MIN <= left <= LEFT_MAX:
                continue
            B = m["book"]
            if scn == "dublin":
                f = seen_fns.setdefault(e, seen_local(m))
                i = f(dec)
                arr = dec + send_ms
            else:
                i = d.state(m, dec - (TOKYO if scn == "tokyo" else COLO)["book"], strict=False)
                arr = dec + (TOKYO if scn == "tokyo" else COLO)["send"]
            ask = seen_ask(B, i, tok)
            if ask is None or not ASK_MIN <= ask <= ASK_MAX:
                continue
            match = arr + HOLD
            fl = d.taker(m, int(np.ceil(match)), tok, ask + CAP, SIZE)  # real order: other takers first, nothing excluded
            w = winners.get(d.slug(("btc", 5, e)))
            row = dict(src=src, scn=scn, T=T, decide=round(dec, 1), match=int(np.ceil(match)), e=e, left_s=round(left, 1),
                       tl="30-120" if left < 120 else "120-300", sgn=sgn, tok=tok, seen_ask=ask, limit=ask + CAP,
                       filled=int(fl is not None), winner=w or "")
            if fl:
                fee_c = 100 * fl["fee"] / fl["size"]
                mid2 = d.mid(m, fl["x"] + 2000, tok)
                row.update(px=fl["px"], size=fl["size"], fee_c=round(fee_c, 4),
                           pnl_c=round((100.0 if (w == "UP") == (tok == 0) else 0.0) - fl["px"] / 100 - fee_c, 4) if w else "",
                           mo2s_c=round((mid2 - fl["px"]) / 100 - fee_c, 4) if mid2 is not None else "")
            rows.append(row)
    return rows


def run(day, root=d.REC, out=OUTD, probe=Path.home() / "order-probe.csv", winners=None):
    if day < FIRST_DAY:
        raise SystemExit("R-D3 counts from 2026-11-15")
    mk, _, bn = d.load_day(day, root=root, asset="btc", tf=5)
    W = winners if winners is not None else d.fetch_winners(sorted(d.slug(k) for k in mk))
    have = sum(d.slug(k) in W for k in mk)
    old = time.time() - time.mktime(time.strptime(day, "%Y-%m-%d")) > 4 * 86400  # stuck market: write after 3 full days
    if have < len(mk) and not old:  # the day's windows are not all settled yet: retry later
        print(f"{day}: winners {have}/{len(mk)}, not writing", flush=True)
        return 2
    send = send_p50(day, probe)
    rows = []
    for name, src in SRC.items():
        rows += evaluate(mk, signals(bn, src), name, W, send)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / f"rd3-{day}.csv", index=False)
    print(f"{day}: wrote {len(rows)} rows", flush=True)  # row count only: the test is blind
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    sys.exit(run(ap.parse_args().day))
