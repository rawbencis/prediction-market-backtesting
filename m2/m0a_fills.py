"""M2-0a: OhioAI's BTC 5m fills with >= 240 s elapsed, from the recordings of one day, with book/feed context.

Sources (all on this box): census ~/shadow-rec/DAY/act-HH.bin (one row per participant; fee > 0 = taker),
prints tr-HH.bin (exchange ms, joined by tx hash), top of book pm-HH.bin (UP token; DOWN = mirror),
~/feeds-rec/DAY/{chainlink,twap60}-HH.jsonl.gz.

Per fill (one census row of the wallet, BUY or SELL):
  T        match time, exchange ms: the print with the same tx hash (min x); fallback trade_ts * 1000 (tjoin = 0)
  left     seconds left = e + 300 - T/1000
  role     taker (census fee > 0) / maker; fee = census fee (USDC), cost = p * size + fee for a BUY
  fv       TWAP-aware fair value of the side bought, the d2_samples.py --final formula: Chainlink stream by local
           receipt second as S (forward-filled, 1800 s history, >= 900 s present), twap60 row stamped x = window start
           (+-2 s) as K. NaN when either feed is missing.
  kgap     strike gap, bp: 1e4 * (K - CL_x(start)) / CL_x(start), CL_x = Chainlink row stamped x = start second (+-2 s);
           signed toward the side bought: Down keeps the sign, Up flips it. Positive = the strike sits on the side
           bought's favourable side of spot at the start (Up: K below spot; Down: K above spot).
  age_ms   (takers) how long the side's best ask had been continuously <= the fill price before the match. The print's
           exchange stamp trails the book changes of its own match burst by ~30 ms (median; p90 ~80 ms), so the run is
           taken to end at the last pm row (by exchange ms, x < T) with ask <= price, if that row is within 250 ms of T
           (lastgap_ms = T - that row); else age = 0. bid=ask=0 rows break the run. 'stood' flags: age >= 500 / >= 2000.
  ask_1s   the side's best ask 1 s before T.
  rem500 / rem2000: the side's best ask is still <= the fill price at T + 500 ms / T + 2 s.
  oth_n / oth_sh / othb_n: other wallets' taker rows on the same token within +-1 s of T (time from their print by tx
           hash, else their trade_ts), all sides / BUY only.
The wallet 0xc387c2a4 is dropped at load (rows dated 11-24+ are embargoed; it is dropped on all days for simplicity).
No Binance data is read.

usage: m0a_fills.py DAY OUT.csv
"""
import csv
import gzip
import math
import sys
from pathlib import Path

import numpy as np
import orjson

sys.path.insert(0, str(Path.home() / "lighter-flips"))
import act_rec  # noqa: E402

REC, FEEDS = Path.home() / "shadow-rec", Path.home() / "feeds-rec"
W = bytes.fromhex("129246beaac356e104c397f0985c11b7469be5a6")
EMB = bytes.fromhex("c387c2a4")
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("tok", "u1"), ("sd", "u1"),
                  ("p", "<u2"), ("sz", "<f4"), ("fee", "<u2"), ("tx", "S32")])
LAST_DAY = "2026-11-27"


def day_add(d, n):
    return str(np.datetime64(d) + np.timedelta64(n, "D"))


def read_bin(day, h, kind, dt):
    f = REC / day / f"{kind}-{h:02d}.bin"
    if not f.exists():
        return np.zeros(0, dt)
    return np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)


def hours_of(day, extra_next=True):
    out = [(day, h) for h in range(24)]
    if extra_next and day_add(day, 1) <= LAST_DAY:
        out.append((day_add(day, 1), 0))
    return out


def feed(day, name, asset="btc", by="x"):
    out = {}
    for d in (day_add(day, -1), day, day_add(day, 1)):
        if d > LAST_DAY:
            continue
        pat = f"{name}-23.jsonl.gz" if d < day else f"{name}-00.jsonl.gz" if d > day else f"{name}-*.jsonl.gz"
        for f in sorted((FEEDS / d).glob(pat)):
            try:
                with gzip.open(f, "rb") as g:
                    for line in g:
                        if b'"s":"' + asset.encode() + b'"' not in line:
                            continue
                        try:
                            m = orjson.loads(line)
                        except orjson.JSONDecodeError:
                            continue
                        if by == "x":
                            out[int(m["x"]) // 1000] = float(m["v"])
                        else:
                            out[int(m["r"]) // 1_000_000_000] = float(m["v"])
            except (EOFError, OSError, __import__("zlib").error):
                continue
    return out


def Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ffill(v):
    out, last = [], np.nan
    for x in v:
        last = x if x == x else last
        out.append(last)
    return np.array(out)


def fv_up(cl, ts, tau, K):
    """d2_samples.py --final Chainlink fair value of UP at second ts with tau seconds left (copied formula)."""
    cs = ffill([cl.get(s, np.nan) for s in range(ts - 1800, ts + 1)])
    if not (np.isfinite(cs[-1]) and np.isfinite(cs[:-1]).sum() > 900):
        return float("nan")
    sig = np.nanstd(np.diff(np.log(cs)))
    Rc = np.nanmean(cs[len(cs) - 1 - int(60 - tau):]) if tau < 60 else cs[-1]
    Sc = ((60 - tau) * Rc + tau * cs[-1]) / 60 if tau < 60 else cs[-1]
    sdc = sig * math.sqrt(tau ** 3 / 10800) if tau < 60 else sig * math.sqrt(tau - 40)
    return Phi(math.log(Sc / K) / sdc) if sdc > 0 else float("nan")


def near(d, s, k=2):
    for o in [0] + [x for i in range(1, k + 1) for x in (-i, i)]:
        if s + o in d:
            return d[s + o]
    return None


def main():
    day, outp = sys.argv[1], sys.argv[2]
    d0 = int((np.datetime64(day) - np.datetime64("1970-01-01")) / np.timedelta64(1, "s"))
    act = []
    for d, h in hours_of(day):
        f = REC / d / f"act-{h:02d}.bin"
        if f.exists():
            x = np.fromfile(f, dtype=np.dtype(act_rec.ACT_DT), count=f.stat().st_size // np.dtype(act_rec.ACT_DT).itemsize)
            x = x[(x["a"] == 0) & (x["tf"] == 5) & (x["e"] >= d0) & (x["e"] < d0 + 86400)
                  & ((x["ts"].astype(np.int64) - x["e"].astype(np.int64)) >= 230) & ((x["w"] == W) | (x["fee"] > 0))]
            x = x[np.array([not w.startswith(EMB) for w in x["w"].tolist()], bool)] if len(x) else x
            act.append(x)
    act = np.concatenate(act) if act else np.zeros(0, np.dtype(act_rec.ACT_DT))
    mine = act[(act["w"] == W) & ((act["ts"].astype(np.int64) - act["e"].astype(np.int64)) >= 238)]
    if not len(mine):
        open(outp, "w").write("")
        print(day, "no late fills")
        return
    tr = []
    for d, h in hours_of(day):
        x = read_bin(d, h, "tr", TR_DT)
        tr.append(x[(x["a"] == 0) & (x["tf"] == 5) & (x["x"] >= (x["e"].astype(np.int64) + 225) * 1000)])
    tr = np.concatenate(tr)
    txx = {}
    for t, x in zip(tr["tx"].tolist(), tr["x"].tolist()):
        if t not in txx or x < txx[t]:
            txx[t] = x
    # other takers: all taker rows of other wallets, with ms time
    tk = act[(act["fee"] > 0) & (act["w"] != W)]
    tk_T = np.array([txx.get(t, s * 1000) for t, s in zip(tk["tx"].tolist(), tk["ts"].tolist())], np.int64)
    twap = feed(day, "twap60")
    clx = feed(day, "chainlink", by="x")
    clr = feed(day, "chainlink", by="r")
    pm_cache = {}

    def pm_for(e):
        hs = sorted({(e // 3600) * 3600, ((e + 300 + 5) // 3600) * 3600})
        rows = []
        for h0 in hs:
            if h0 not in pm_cache:
                dd = str(np.datetime64(h0, "s").astype("datetime64[D]"))
                hh = int((h0 % 86400) // 3600)
                p = np.concatenate([read_bin(dd, hh, "pm", PM_DT),
                                    read_bin(*((dd, hh + 1) if hh < 23 else (day_add(dd, 1), 0)), "pm", PM_DT)
                                    if (dd, hh + 1) <= (LAST_DAY, 23) else np.zeros(0, PM_DT)])
                pm_cache.clear() if len(pm_cache) > 3 else None
                pm_cache[h0] = p[(p["a"] == 0) & (p["tf"] == 5) & (p["x"] >= (p["e"].astype(np.int64) + 180) * 1000)]
            rows.append(pm_cache[h0][pm_cache[h0]["e"] == e])
        p = np.concatenate(rows)
        _, ui = np.unique(np.stack([p["l"], p["x"]], 1), axis=0, return_index=True)
        p = p[np.sort(ui)]
        return p[np.argsort(p["x"], kind="stable")]

    out = []
    for r in mine:
        e, tok, sd, p, sz = int(r["e"]), int(r["tok"]), int(r["sd"]), float(r["p"]), float(r["sz"])
        tx = r["tx"]
        T = txx.get(tx)
        tjoin = T is not None
        T = T if tjoin else int(r["ts"]) * 1000
        left = e + 300 - T / 1000
        if left > 60.0001 or left < 0:
            continue
        taker = float(r["fee"]) > 0
        fee = float(r["fee"]) if taker else 0.0
        K = near(twap, e)
        c0 = near(clx, e)
        fvu = fv_up(clr, T // 1000, left, K) if K else float("nan")
        fv = fvu if tok == 0 else 1 - fvu
        kgap = float("nan")
        if K and c0:
            kgap = 1e4 * (K - c0) / c0 * (1 if tok == 1 else -1)
        age = rem5 = rem2 = lastgap = ask1s = float("nan")
        pmr = pm_for(e)
        if len(pmr):
            if tok == 0:
                ask = np.where((pmr["k"] > 0) & (pmr["k"] < 10000), pmr["k"] / 1e4, 9.0)
            else:
                ask = np.where((pmr["b"] > 0) & (pmr["b"] < 10000), 1 - pmr["b"] / 1e4, 9.0)
            ok = ask <= p + 1e-9
            i = int(np.searchsorted(pmr["x"], T, "left")) - 1
            i1 = int(np.searchsorted(pmr["x"], T - 1000, "right")) - 1
            ask1s = float(ask[i1]) if i1 >= 0 and ask[i1] < 9 else float("nan")
            if i >= 0 and taker and sd == 0:
                okj = np.flatnonzero(ok[: i + 1])
                if len(okj) and T - pmr["x"][okj[-1]] <= 250:
                    j = okj[-1]
                    bad = np.flatnonzero(~ok[: j + 1])
                    start = bad[-1] + 1 if len(bad) else 0
                    age, lastgap = float(pmr["x"][j] - pmr["x"][start]), float(T - pmr["x"][j])
                else:
                    age, lastgap = 0.0, float("nan")
            for dt, nm in ((500, "rem5"), (2000, "rem2")):
                j = int(np.searchsorted(pmr["x"], T + dt, "right")) - 1
                v = float(ok[j]) if j >= 0 else float("nan")
                if nm == "rem5":
                    rem5 = v
                else:
                    rem2 = v
        sel = (tk["e"] == e) & (tk["tok"] == tok) & (np.abs(tk_T - T) <= 1000)
        oth_n, oth_sh = int(sel.sum()), float(tk["sz"][sel].sum())
        othb = int((sel & (tk["sd"] == 0)).sum())
        out.append(dict(day=day, e=e, slug=f"btc-updown-5m-{e}", tok=tok, side="BUY" if sd == 0 else "SELL",
                        T=T, tjoin=int(tjoin), left=round(left, 3), p=p, sz=sz, fee=fee, role="taker" if taker else "maker",
                        K=K or "", fv=fv, kgap=kgap, age_ms=age, lastgap_ms=lastgap, ask_1s=ask1s, rem500=rem5, rem2000=rem2,
                        oth_n=oth_n, oth_sh=oth_sh, othb_n=othb, tx=tx.hex()))
    with open(outp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]) if out else ["day"])
        w.writeheader()
        w.writerows(out)
    print(day, "late fills", len(out), "tx-joined", sum(o["tjoin"] for o in out), "fv ok", sum(o["fv"] == o["fv"] for o in out),
          flush=True)


if __name__ == "__main__":
    main()
