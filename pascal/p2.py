"""P2: Pascal trade history against Polymarket (descriptive; selects nothing).

fetch: for the 30 mapped markets with the most Pascal all-time taker notional (11-27 snapshot), page back
       through GET /api/v1/trades (limit 500) until the last page or HISTORY_NOT_READY; Polymarket long-token
       price history from CLOB GET /prices-history (fidelity = 1 minute, 1-day chunks) from the first
       Pascal trade to now. <= 2 requests/s. Raw JSON under ~/pascal-data/raw/p2/.
table: taker edge vs Polymarket at the trade and 1/10/60 min later by taker wallet; concentration of takers
       and makers; share of volume trading toward / away from Polymarket's price; what a maker at
       Polymarket +- h captured per share before and after a Polymarket hedge fee.
"""

import argparse
import gzip
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HOME = Path.home()
RAW = HOME / "pascal-data" / "raw" / "p2"
SNAP = HOME / "pascal-data" / "raw" / "snap" / "markets_20261127.json"
GAMMA = HOME / "pascal-data" / "raw" / "census" / "gamma.json.gz"
UA = {"User-Agent": "pascal-p2 research (read-only)"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    for i in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            if "HISTORY_NOT_READY" in body:
                return {"_history_not_ready": True}
            if e.code in (429, 500, 502, 503) and i < 4:
                time.sleep(3 * (i + 1))
                continue
            raise
    return None


def vol(m, k):
    s = (m.get("stats") or {}).get(k) or {}
    return float(s.get("taker_buy_volume_notional") or 0) + float(s.get("taker_sell_volume_notional") or 0)


def top_markets(n=30):
    ms = json.load(open(SNAP))["data"]
    mp = [m for m in ms if ((m.get("display_attributes") or {}).get("reference") or {}).get("kind") == "polygon"]
    mp.sort(key=lambda m: -vol(m, "all_time"))
    return mp[:n]


def fetch(n=30):
    RAW.mkdir(parents=True, exist_ok=True)
    for m in top_markets(n):
        s = m["symbol"]
        out = RAW / f"{s}.trades.json.gz"
        if not out.exists():
            items, cur, stop = [], None, "last_page"
            while True:
                q = {"symbols": s, "limit": 500}
                if cur:
                    q["before_cursor"] = cur
                r = get("https://data.pascal.trade/api/v1/trades?" + urllib.parse.urlencode(q))
                time.sleep(0.6)
                if r is None or r.get("_history_not_ready"):
                    stop = "HISTORY_NOT_READY"
                    break
                d = r["data"]
                items += d["items"]
                cur = d.get("next_cursor")
                if not cur:
                    break
            with gzip.open(out, "wt") as f:
                json.dump({"symbol": s, "stop": stop, "items": items}, f)
            print(f"{s}: {len(items)} trades ({stop})", flush=True)
        tok = str(m["display_attributes"]["reference"]["market_outcome_token_id"])
        hp = RAW / f"{s}.pm.json.gz"
        if hp.exists():
            continue
        with gzip.open(out, "rt") as f:
            tr = json.load(f)["items"]
        if not tr:
            continue
        t0 = min(int(x["trade_ts_ms"]) for x in tr) // 1000 - 3600
        t1 = int(time.time())
        hist = []
        t = t0
        while t < t1:
            r = get("https://clob.polymarket.com/prices-history?" + urllib.parse.urlencode(
                {"market": tok, "startTs": t, "endTs": min(t + 86400, t1), "fidelity": 1}))
            time.sleep(0.6)
            hist += (r or {}).get("history") or []
            t += 86400
        with gzip.open(hp, "wt") as f:
            json.dump({"symbol": s, "token": tok, "history": hist}, f)
        print(f"{s}: pm history {len(hist)} points", flush=True)


def table(hs=(0.01, 0.02, 0.03)):
    ms = {m["symbol"]: m for m in top_markets(30)}
    with gzip.open(GAMMA, "rt") as f:
        gm = json.load(f)
    rows = []
    span = {}
    for s, m in ms.items():
        p = RAW / f"{s}.trades.json.gz"
        hpth = RAW / f"{s}.pm.json.gz"
        if not p.exists() or not hpth.exists():
            continue
        tr = json.load(gzip.open(p, "rt"))
        hi = json.load(gzip.open(hpth, "rt"))["history"]
        if not hi:
            continue
        ht = np.array([h["t"] for h in hi], dtype=np.float64)
        hp_ = np.array([h["p"] for h in hi])
        o = np.argsort(ht)
        ht, hp_ = ht[o], hp_[o]
        g = gm.get(m["display_attributes"]["reference"]["condition_id"]) or {}
        fs = g.get("feeSchedule") or {}
        rm = float(fs.get("rate") or 0) if g.get("feesEnabled") else 0.0
        rp = float(m["taker_fee_rate"])

        def pm_at(t):
            i = np.searchsorted(ht, t, side="right") - 1
            return hp_[i] if i >= 0 and t - ht[i] <= 180 else math.nan  # stale > 3 min: unknown

        ts_all = [int(x["trade_ts_ms"]) for x in tr["items"]]
        span[s] = (min(ts_all), max(ts_all), tr["stop"], len(ts_all)) if ts_all else None
        for x in tr["items"]:
            t = int(x["trade_ts_ms"]) / 1000
            pr, sz = float(x["price"]), float(x["size"])
            sgn = 1.0 if x["taker_side"] == "BID" else -1.0  # taker bought long
            m0 = pm_at(t)
            r = dict(sym=s, t=t, p=pr, sz=sz, sgn=sgn, taker=x["taker"], maker=x["maker"], rm=rm, rp=rp,
                     pm0=m0, pm1=pm_at(t + 60), pm10=pm_at(t + 600), pm60=pm_at(t + 3600))
            rows.append(r)
    out = []
    P = out.append
    P("### P2: Pascal trade history vs Polymarket (top 30 mapped markets by Pascal all-time notional)\n")
    first = min(v[0] for v in span.values() if v)
    P(f"History depth: earliest Pascal trade {time.strftime('%Y-%m-%d', time.gmtime(first / 1000))}; "
      f"pagination stop reasons {dict(Counter(v[2] for v in span.values() if v))}; trades {len(rows):,}. Polymarket "
      f"reference: CLOB `/prices-history` (long token, fidelity 1 minute; a point older than 3 min counts as unknown).\n")
    ok = [r for r in rows if r["pm0"] == r["pm0"]]
    vol_ = sum(r["sz"] * r["p"] for r in rows)
    P(f"Trades with a Polymarket price within 3 min: {len(ok):,} of {len(rows):,} "
      f"(${sum(r['sz'] * r['p'] for r in ok):,.0f} of ${vol_:,.0f} notional).\n")

    def edge(r, k):
        return r["sgn"] * (r[k] - r["p"])  # taker's edge per share vs PM price (long terms)

    P("Taker edge vs Polymarket (¢/share, size-weighted, before fees; + = taker bought below / sold above PM):\n")
    P("| set | trades | shares | at trade | +1 min | +10 min | +60 min |")
    P("|---|---:|---:|---:|---:|---:|---:|")

    def line(name, rs):
        cells = []
        for k in ("pm0", "pm1", "pm10", "pm60"):
            w = [(r["sz"], edge(r, k)) for r in rs if r[k] == r[k]]
            cells.append(f"{100 * sum(a * b for a, b in w) / max(sum(a for a, _ in w), 1e-9):.2f}" if w else "-")
        P(f"| {name} | {len(rs):,} | {sum(r['sz'] for r in rs):,.0f} | " + " | ".join(cells) + " |")

    line("all", ok)
    tk = Counter()
    for r in ok:
        tk[r["taker"]] += r["sz"] * r["p"]
    tot = sum(tk.values())
    for w, v in tk.most_common(8):
        line(f"taker {w[:6]}… ({v / tot:.0%} of $)", [r for r in ok if r["taker"] == w])
    mk = Counter()
    for r in ok:
        mk[r["maker"]] += r["sz"] * r["p"]
    P("")
    P(f"Concentration: takers {len(tk)} wallets, top 1 {tk.most_common(1)[0][1] / tot:.0%}, top 3 "
      f"{sum(v for _, v in tk.most_common(3)) / tot:.0%}; makers {len(mk)} wallets, top 1 "
      f"{mk.most_common(1)[0][1] / tot:.0%}, top 3 {sum(v for _, v in mk.most_common(3)) / tot:.0%}.")
    toward = [r for r in ok if edge(r, "pm0") > 0.0049]
    away = [r for r in ok if edge(r, "pm0") < -0.0049]
    flat = [r for r in ok if abs(edge(r, "pm0")) <= 0.0049]
    sh = sum(r["sz"] for r in ok)
    P(f"\nToward Polymarket's price (taker edge > 0.5¢, arb-like): {sum(r['sz'] for r in toward) / sh:.0%} of shares; "
      f"away (< -0.5¢): {sum(r['sz'] for r in away) / sh:.0%}; within ±0.5¢: {sum(r['sz'] for r in flat) / sh:.0%}.\n")
    P("A Pascal maker quoting Polymarket ± h (fills: trades at or through our price), ¢ per filled share "
      "(maker side = −taker edge, vs PM price at the trade; hedge fee = PM rate × p(1 − p)):\n")
    P("| h | filled trades | shares | captured vs PM at trade | after PM hedge fee | vs PM +10 min |")
    P("|---|---:|---:|---:|---:|---:|")
    for h in hs:
        fills = [r for r in ok if -edge(r, "pm0") >= h - 1e-9]
        s_ = sum(r["sz"] for r in fills)
        cap = sum(r["sz"] * h for r in fills)
        fee = sum(r["sz"] * r["rm"] * r["pm0"] * (1 - r["pm0"]) for r in fills)
        w10 = [(r["sz"], -edge(r, "pm10") - (-edge(r, "pm0")) + h) for r in fills if r["pm10"] == r["pm10"]]
        P(f"| {h * 100:.0f}¢ | {len(fills):,} | {s_:,.0f} | {100 * cap / max(s_, 1e-9):.2f} | "
          f"{100 * (cap - fee) / max(s_, 1e-9):.2f} | "
          f"{(100 * sum(a * b for a, b in w10) / max(sum(a for a, _ in w10), 1e-9)) if w10 else float('nan'):.2f} |")
    md = "\n".join(out)
    Path("reports/tokyo").mkdir(parents=True, exist_ok=True)
    Path("reports/tokyo/p2_pascal_history.md").write_text(md + "\n")
    print(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "table"])
    a = ap.parse_args()
    fetch() if a.cmd == "fetch" else table()


if __name__ == "__main__":
    main()
