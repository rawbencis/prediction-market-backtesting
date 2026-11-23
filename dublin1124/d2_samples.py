"""D2 sampler: level of the underlying vs the up/down book, one row per (window, side, sample time). Streams hour files.

Shared grid definitions (Dublin side):
  S      = Binance spot (src 0) as received here: last trade with local receipt time <= t.
  K      = Chainlink 60 s TWAP at the window's start second from ~/feeds-rec twap60 (nearest x within +-2 s), else
           the mean of the per-second S over the 60 s before the start (ksrc 1 = Chainlink, 0 = Binance proxy).
  sigma_l / sigma_s = st. dev. of 1 s log returns of the per-second S over the previous 1800 s / 60 s.
  tau >= 60: tau_eff = tau - 40, sd = sigma_l * sqrt(tau_eff), S unchanged.
  tau <  60: S' = ((60 - tau) R + tau S) / 60, R = mean of per-second S over the elapsed part of the last minute;
             sd = sigma_l * sqrt(tau^3 / 10800).
  d = 1e4 ln(S'/K) toward the side (bp), z = (d / 1e4) / sd, r = sigma_s / sigma_l.
Execution (`live`, a taker sent in a calm moment): decision at local time t sees book rows received by t; the FAK BUY
(limit = ask seen) matches at exchange time t + 205 ms (9 + 55 + 150 - 9 receive offset) against the standing top of
book: fills if that ask <= limit with >= 5 shown, pays that ask. Pessimistic fill: the paid price must still show >= 5
shares at exchange time t + 305 ms. Mode --final: 1 s samples for tau 60..5 (final-minute table) and a Chainlink-based
TWAP-aware fair value (Chainlink stream as S, twap60 as K) beside the Binance one.

usage: d2_samples.py --asset btc --tf 5 --days D [D ...] --out OUT.npz [--final]
"""
import argparse
import gzip
import math
from pathlib import Path

import numpy as np
import orjson

REC = Path.home() / "shadow-rec"
FEEDS = Path.home() / "feeds-rec"
ASSETS = ("btc", "eth", "sol", "xrp", "doge")
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
MATCH_MS, PESS_MS = 205, 305


def hour_file(day, h, kind):
    return REC / day / f"{kind}-{h:02d}.bin"


def prev_hour(day, h):
    if h > 0:
        return day, h - 1
    d = np.datetime64(day) - np.timedelta64(1, "D")
    return str(d), 23


def next_hour(day, h):
    if h < 23:
        return day, h + 1
    return str(np.datetime64(day) + np.timedelta64(1, "D")), 0


def read(day, h, kind, dt, keep):
    f = hour_file(day, h, kind)
    if not f.exists():
        return np.zeros(0, dt)
    x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
    return x[keep(x)]


def feed_series(day, feed, asset, by="x"):
    """{second: value} from a feeds-rec feed for one asset (x = venue ms, r = local ns)."""
    out = {}
    for f in sorted((FEEDS / day).glob(f"{feed}-*.jsonl.gz")):
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
                    else:  # local receipt second -> latest value
                        out[int(m["r"]) // 1_000_000_000] = float(m["v"])
        except (EOFError, OSError, __import__("zlib").error):  # truncated gzip member (feeds-rec restarts): keep what was read
            continue
    return out


def load_settle():
    import csv
    return {r["slug"]: r["winner"] for r in csv.DictReader(open(Path.home() / "dublin-1124/settle.csv"))}


def Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--tf", type=int, default=5)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--final", action="store_true")
    a = ap.parse_args()
    ai, W = ASSETS.index(a.asset), 60 * a.tf
    win = load_settle()
    rows = []
    for day in a.days:
        twap = feed_series(day, "twap60", a.asset)
        twap.update({k: v for k, v in feed_series(str(np.datetime64(day) - np.timedelta64(1, "D")), "twap60", a.asset).items()
                     if k not in twap} if not twap else {})
        cl = feed_series(day, "chainlink", a.asset, by="r") if a.final else {}
        bn_prev = read(*prev_hour(day, 0), "bn", BN_DT, lambda x: (x["src"] == 0) & (x["a"] == ai))
        for h in range(24):
            bn_cur = read(day, h, "bn", BN_DT, lambda x: (x["src"] == 0) & (x["a"] == ai))
            if not len(bn_cur):
                bn_prev = bn_cur
                continue
            bn = np.concatenate([bn_prev, bn_cur])
            bn = bn[np.argsort(bn["l"], kind="stable")]
            bl, bp = bn["l"] // 1000, bn["p"]
            h0 = int((np.datetime64(f"{day}T{h:02d}:00") - np.datetime64("1970-01-01T00:00")) / np.timedelta64(1, "s"))
            eps = [e for e in range(h0, h0 + 3600, W)]
            pm = np.concatenate([read(day, h, "pm", PM_DT, lambda x: (x["a"] == ai) & (x["tf"] == a.tf)),
                                 read(*next_hour(day, h), "pm", PM_DT, lambda x: (x["a"] == ai) & (x["tf"] == a.tf) & (x["x"] < (h0 + 3600 + 120) * 1000))])
            for e in eps:
                key = f"{a.asset}-updown-{a.tf}m-{e}"
                if key not in win:
                    continue
                up_won = win[key] == "UP"
                p = pm[pm["e"] == e]
                if len(p) < 10:
                    continue
                pl = p[np.argsort(p["l"], kind="stable")]
                px = p[np.argsort(p["x"], kind="stable")]
                e_ms, end = e * 1000, (e + W) * 1000
                secs = np.arange(e_ms - 1_861_000, end + 1, 1000)
                i = np.searchsorted(bl, secs, "right") - 1
                if (i < 0).any() or bl[0] > e_ms - 1_800_000:
                    continue  # not enough Binance history (start of the recording)
                Ssec = bp[i]
                lr = np.diff(np.log(Ssec))  # lr[j] = return over (secs[j], secs[j+1]]
                c1, c2 = np.r_[0, np.cumsum(lr)], np.r_[0, np.cumsum(lr * lr)]

                def sd_ret(j, n):  # st. dev. of the n returns ending at secs[j]
                    s1, s2 = c1[j] - c1[j - n], c2[j] - c2[j - n]
                    v = (s2 - s1 * s1 / n) / (n - 1)
                    return math.sqrt(v) if v > 0 else float("nan")

                j0 = int(np.searchsorted(secs, e_ms))
                k_tw = next((twap[s] for s in (e, e - 1, e + 1, e - 2, e + 2) if s in twap), None)
                K, ksrc = (k_tw, 1) if k_tw else (float(Ssec[j0 - 60:j0].mean()), 0)
                if a.final:
                    tlist = [end - 1000 * s for s in range(60, 4, -1)]
                else:
                    tlist = list(range(e_ms + 10_000, end - 10_000 + 1, 5000))
                for t in tlist:
                    tau = (end - t) / 1000
                    jt = int(np.searchsorted(secs, t, "right")) - 1
                    ib = int(np.searchsorted(bl, t, "right")) - 1
                    S = float(bp[ib])
                    S1 = float(bp[max(0, int(np.searchsorted(bl, t - 1000, "right")) - 1)])
                    sl, ss = sd_ret(jt, 1800), sd_ret(jt, 60)
                    if not (sl > 0):
                        continue
                    if tau >= 60:
                        Sx, sd = S, sl * math.sqrt(tau - 40)
                    else:
                        js = int(np.searchsorted(secs, end - 60_000))
                        R = float(Ssec[js:jt + 1].mean()) if jt >= js else S
                        Sx, sd = ((60 - tau) * R + tau * S) / 60, sl * math.sqrt(tau ** 3 / 10800)
                    d_up = 1e4 * math.log(Sx / K)
                    z_up = d_up / 1e4 / sd
                    mv1 = 1e4 * math.log(S / S1)
                    fv_cl = float("nan")
                    if a.final and k_tw:
                        ts = t // 1000
                        cs = [cl.get(s) for s in range(ts - 1800, ts + 1)]
                        cs = np.array([np.nan if v is None else v for v in cs])
                        cs = np.array(pd_ffill(cs))
                        if np.isfinite(cs[-1]) and np.isfinite(cs[:-1]).sum() > 900:
                            sig = np.nanstd(np.diff(np.log(cs)))
                            Rc = np.nanmean(cs[len(cs) - 1 - int(60 - tau):]) if tau < 60 else cs[-1]
                            Sc = ((60 - tau) * Rc + tau * cs[-1]) / 60 if tau < 60 else cs[-1]
                            sdc = sig * math.sqrt(tau ** 3 / 10800) if tau < 60 else sig * math.sqrt(tau - 40)
                            fv_cl = Phi(math.log(Sc / k_tw) / sdc) if sdc > 0 else float("nan")
                    iq = int(np.searchsorted(pl["l"] // 1000, t, "right")) - 1
                    im = int(np.searchsorted(px["x"], t + MATCH_MS, "right")) - 1
                    ip = int(np.searchsorted(px["x"], t + PESS_MS, "right")) - 1
                    if iq < 0 or im < 0:
                        continue
                    b, k = int(pl["b"][iq]), int(pl["k"][iq])
                    if 0 < k <= b < 10000:  # crossed
                        continue
                    two = 0 < b < k < 10000
                    mb, mk = int(px["b"][im]), int(px["k"][im])
                    qb, qk = int(px["b"][ip]), int(px["k"][ip])
                    for side in (0, 1):  # 0 UP, 1 DOWN
                        sg = 1 if side == 0 else -1
                        if (side == 0 and not 0 < k < 10000) or (side == 1 and not 0 < b < 10000):
                            continue  # no ask on this side
                        if side == 0:
                            ask, asz, mid = k / 1e4, float(pl["ks"][iq]), ((b + k) / 2e4 if two else np.nan)
                            mask_, msz = (mk / 1e4 if 0 < mk < 10000 else 9.0), float(px["ks"][im])
                            pask, psz = (qk / 1e4 if 0 < qk < 10000 else 9.0), float(px["ks"][ip])
                        else:
                            ask, asz, mid = 1 - b / 1e4, float(pl["bs"][iq]), (1 - (b + k) / 2e4 if two else np.nan)
                            mask_, msz = (1 - mb / 1e4 if 0 < mb < 10000 else 9.0), float(px["bs"][im])
                            pask, psz = (1 - qb / 1e4 if 0 < qb < 10000 else 9.0), float(px["bs"][ip])
                        filled = asz >= 5 and mask_ <= ask + 1e-9 and msz >= 5
                        pess = filled and pask <= mask_ + 1e-9 and psz >= 5
                        z = sg * z_up
                        rows.append((e, side, t, tau, sg * d_up, z, ss / sl if ss == ss else np.nan, sl, Phi(z), mid, ask, asz,
                                     filled, mask_ if filled else np.nan, pess, float(up_won == (side == 0)), sg * mv1, ksrc,
                                     (fv_cl if side == 0 else 1 - fv_cl), b / 1e4, k / 1e4))
            bn_prev = bn_cur
        print(a.asset, a.tf, day, len(rows), flush=True)
    cols = ("e", "side", "t", "tau", "d", "z", "r", "sl", "phi", "mid", "ask", "asz", "filled", "paid", "pess", "win", "mv1",
            "ksrc", "fvcl", "upbid", "upask")
    arr = np.array(rows, dtype=float) if rows else np.zeros((0, len(cols)))
    np.savez_compressed(a.out, cols=np.array(cols), data=arr)


def pd_ffill(v):
    out, last = [], np.nan
    for x in v:
        last = x if x == x else last
        out.append(last)
    return out


if __name__ == "__main__":
    main()
