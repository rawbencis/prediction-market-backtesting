"""T4.2: BTC 1h up/down history. Gamma (markets, winners), CLOB prices-history (Up token, fidelity 1 min), Binance 1m klines.

  fetch --start E0 --end E1   -> out/t4/h1_markets.csv, out/t4/hist/<e>.json
  klines --months M.. --days D0 D1 -> out/t4/binance/*.zip (data.binance.vision)
  calib                       -> calibration table by z x minutes left x r
Public reads only; <= 1 request/s from this script.
"""

import argparse
import csv
import io
import json
import time
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent / "out" / "t4"
UA = {"User-Agent": "research"}
last = [0.0]


def get(url, raw=False):
    w = 1.0 - (time.time() - last[0])
    if w > 0:
        time.sleep(w)
    last[0] = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        b = r.read()
    return b if raw else json.loads(b)


NEW_FROM = 1794960000  # 2026-11-18 00:00 UTC: btc-updown-1h-<epoch>, Chainlink TWAP; before: Binance 1h candle, title slug


def slug(e):
    if e >= NEW_FROM:
        return f"btc-updown-1h-{e}"
    d = datetime.fromtimestamp(e, ZoneInfo("America/New_York"))
    h = d.hour % 12 or 12
    return f"bitcoin-up-or-down-{d.strftime('%B').lower()}-{d.day}-{d.year}-{h}{'am' if d.hour < 12 else 'pm'}-et"


def cmd_fetch(a):
    (OUT / "hist").mkdir(parents=True, exist_ok=True)
    mp = OUT / "h1_markets.csv"
    have = {}
    if mp.exists():
        with mp.open() as f:
            have = {int(r[0]): r for r in csv.reader(f)}
    es = [e for e in range(a.start, a.end + 1, 3600) if e not in have]
    for i in range(0, len(es), 40):
        sl = {slug(e): e for e in es[i:i + 40]}
        try:
            rows = get("https://gamma-api.polymarket.com/markets?closed=true&limit=100&" + "&".join(f"slug={s}" for s in sl))
        except Exception as ex:  # noqa: BLE001
            print("gamma", ex)
            continue
        for m in rows:
            if m.get("slug") not in sl:
                continue
            names = [n.upper() for n in json.loads(m["outcomes"])]
            pr = [float(v) for v in json.loads(m["outcomePrices"])]
            toks = json.loads(m["clobTokenIds"])
            w = float(names[pr.index(max(pr))] == "UP") if max(pr) >= 0.99 else ""
            have[sl[m["slug"]]] = [sl[m["slug"]], toks[names.index("UP")], w, (m.get("feeSchedule") or {}).get("rate", "")]
    with mp.open("w", newline="") as f:
        csv.writer(f).writerows(v for _, v in sorted(have.items()))
    print(f"markets: {len(have)}")
    n = 0
    for e, r in sorted(have.items()):
        p = OUT / "hist" / f"{e}.json"
        if p.exists():
            continue
        try:
            h = get(f"https://clob.polymarket.com/prices-history?market={r[1]}&startTs={e - 120}&endTs={e + 3600}&fidelity=1")
        except Exception as ex:  # noqa: BLE001
            print("hist", e, ex)
            continue
        p.write_text(json.dumps(h.get("history", [])))
        n += 1
        if n % 100 == 0:
            print("hist", n, flush=True)
    print("hist done", n)


def cmd_klines(a):
    d = OUT / "binance"
    d.mkdir(parents=True, exist_ok=True)
    urls = [f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{m}.zip" for m in a.months]
    d0, d1 = date.fromisoformat(a.days[0]), date.fromisoformat(a.days[1])
    while d0 <= d1:
        urls.append(f"https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{d0}.zip")
        d0 += timedelta(days=1)
    for u in urls:
        p = d / u.rsplit("/", 1)[1]
        if p.exists():
            continue
        try:
            p.write_bytes(get(u, raw=True))
        except Exception as ex:  # noqa: BLE001
            print("kl", u, ex)
    print("klines", len(list(d.glob("*.zip"))))


def load_klines():
    t, c = [], []
    for p in sorted((OUT / "binance").glob("*.zip")):
        with zipfile.ZipFile(p) as z:
            for nm in z.namelist():
                for row in csv.reader(io.TextIOWrapper(z.open(nm))):
                    ts = int(row[0])
                    ts = ts // 1000 if ts > 1e14 else ts  # microsecond timestamps in newer files
                    t.append(ts // 1000)
                    c.append(float(row[4]))
    t, c = np.array(t), np.array(c)
    o = np.argsort(t)
    t, c = t[o], c[o]
    u = np.concatenate([[True], t[1:] != t[:-1]])
    return t[u], c[u]


ZB = (-np.inf, -2, -1, -0.3, 0.3, 1, 2, np.inf)
ZL = ("<=-2", "(-2,-1]", "(-1,-0.3]", "(-0.3,0.3]", "(0.3,1]", "(1,2]", ">2")
TB = ((50, 60), (30, 50), (10, 30), (2, 10))
RB = ((0, 0.7), (0.7, 1.4), (1.4, np.inf))


def load_1s():
    sec, px = [], []
    for f in sorted(Path("/home/ubuntu/binance_1s").glob("BTCUSDT-*.npy")):
        x = np.load(f)
        sec.append(x[:, 0].astype(np.int64))
        px.append(x[:, 1])
    sec, px = np.concatenate(sec), np.concatenate(px)
    o = np.argsort(sec)
    return sec[o], px[o]


def cmd_calib(a):
    """z at each prices-history point's own timestamp (no staleness): S = last completed Binance 1 s close before the
    point, K = 1 s close at the open second; sigma from trailing 24 h of 1 m returns; r = trailing 60 min / 24 h."""
    kt, kc = load_klines()  # kt: open time (s) of each 1m bar, kc: its close
    lr = np.diff(np.log(kc))
    lt = kt[1:] + 60  # time at which each return is known (close of the later bar)
    s1, p1 = load_1s()
    with (OUT / "h1_markets.csv").open() as f:
        mk = [r for r in csv.reader(f) if r[2] != ""]
    samples = []  # (e, tau_min, z, r, p, win)
    n_no1s = 0
    for e_, tok, w, fee in mk:
        e, W = int(e_), float(w)
        if (a.regime == "legacy") != (e < NEW_FROM):
            continue
        p_ = OUT / "hist" / f"{e}.json"
        if not p_.exists():
            continue
        h = json.loads(p_.read_text())
        if len(h) < 5:
            continue
        i0 = np.searchsorted(s1, e - 1)
        if i0 >= len(s1) or s1[i0] != e - 1:
            n_no1s += 1
            continue
        K = p1[i0]
        for x in h:
            t, pr = int(x["t"]), float(x["p"])
            tau = (e + 3600 - t) / 60
            if not (2 <= tau < 60):
                continue
            i = np.searchsorted(s1, t - 1)
            if i >= len(s1) or s1[i] != t - 1:
                continue
            S = p1[i]
            j = np.searchsorted(lt, t, "right")
            r24 = lr[max(0, j - 1440):j]
            r60 = lr[max(0, j - 60):j]
            if len(r24) < 1000:
                continue
            sl, ss = r24.std(), r60.std()
            z = np.log(S / K) / (sl * np.sqrt(tau))
            samples.append((e, tau, z, ss / sl, pr, W))
    print(f"markets without 1 s Binance coverage (dropped): {n_no1s}")
    S_ = np.array(samples)
    print(f"markets with data: {len(np.unique(S_[:, 0]))}, samples {len(S_)}; days {(S_[:, 0].max() - S_[:, 0].min()) / 86400:.1f}")
    print("\nminutes left | z | r | markets | mean price | win | edge_mid ± SE | t")
    rows, ncell, sig = [], 0, 0
    for tl, th in TB:
        for zi in range(7):
            for rl, rh in RB:
                m = (S_[:, 1] >= tl) & (S_[:, 1] < th) & (S_[:, 2] > ZB[zi]) & (S_[:, 2] <= ZB[zi + 1]) & (S_[:, 3] >= rl) & (S_[:, 3] < rh)
                X = S_[m]
                if not len(X):
                    continue
                # one sample per market per cell: the first
                _, first = np.unique(X[:, 0], return_index=True)
                X = X[first]
                n = len(X)
                if n < 10:
                    continue
                ed = X[:, 5] - X[:, 4]
                se = ed.std(ddof=1) / np.sqrt(n)
                ncell += 1
                t_ = ed.mean() / se if se > 0 else 0
                sig += abs(t_) >= 2
                rows.append((f"[{tl},{th})", ZL[zi], f"[{rl},{rh})", n, X[:, 4].mean(), X[:, 5].mean(), ed.mean(), se, t_))
    for r in rows:
        print(f"{r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:.3f} | {r[5]:.3f} | {100 * r[6]:+.2f} ± {100 * r[7]:.2f}c | {r[8]:+.2f}")
    print(f"\ncells with n >= 10: {ncell}; |t| >= 2: {sig}; expected by chance ~{0.0455 * ncell:.1f}")
    # pooled over r: by tau x z
    print("\npooled over r: minutes left | z | markets | price | win | edge_mid ± SE")
    for tl, th in TB:
        for zi in range(7):
            m = (S_[:, 1] >= tl) & (S_[:, 1] < th) & (S_[:, 2] > ZB[zi]) & (S_[:, 2] <= ZB[zi + 1])
            X = S_[m]
            _, first = np.unique(X[:, 0], return_index=True)
            X = X[first]
            if len(X) < 10:
                continue
            ed = X[:, 5] - X[:, 4]
            print(f"[{tl},{th}) | {ZL[zi]} | {len(X)} | {X[:, 4].mean():.3f} | {X[:, 5].mean():.3f} | {100 * ed.mean():+.2f} ± {100 * ed.std(ddof=1) / np.sqrt(len(X)):.2f}c")
    np.save(OUT / f"calib_samples_{a.regime}.npy", S_)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("fetch")
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    p = sp.add_parser("klines")
    p.add_argument("--months", nargs="*", default=[])
    p.add_argument("--days", nargs=2, required=True)
    p = sp.add_parser("calib")
    p.add_argument("--regime", choices=("legacy", "new"), default="legacy")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "klines": cmd_klines, "calib": cmd_calib}[a.cmd](a)


if __name__ == "__main__":
    main()
