"""P1: Pascal x Polymarket census from one snapshot. Read-only public endpoints, <= 2 requests/s.

fetch: Pascal markets (given file), Gamma markets by condition_id, Pascal books, Polymarket CLOB books.
       Raw responses are cached under --raw so the tables can be rebuilt offline.
table: per-pair CSV + markdown tables (categories, volume concentration, spreads, gaps now, MECE baskets,
       capacity gate).

Orientation: Pascal long pays $1 iff Polymarket `market_outcome_token_id` wins (docs: ReferenceAttributes).
Gap directions (cost of a $1 pair, after both taker fees; fee(p) = rate * p * (1 - p) per share):
  LP  buy Pascal long at Pascal ask  + buy PM reverse token at its ask
  SP  buy Pascal short (sell at Pascal bid b; costs 1 - b; fee at price b) + buy PM long token at its ask
"""

import argparse
import csv
import gzip
import json
import math
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

UA = {"User-Agent": "pascal-census research (read-only)", "Content-Type": "application/json"}
GAMMA = "https://gamma-api.polymarket.com/markets?"
PBOOKS = "https://data.pascal.trade/api/v1/books?symbols="
CBOOKS = "https://clob.polymarket.com/books"
GAP_CUTS = (0.01, 0.02, 0.04)


def get(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=UA)
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < 3:
                time.sleep(2 * (i + 1))
                continue
            raise


def pace():
    time.sleep(0.6)


def vol(m, k="last_24h"):
    s = (m.get("stats") or {}).get(k) or {}
    return float(s.get("taker_buy_volume_notional") or 0) + float(s.get("taker_sell_volume_notional") or 0)


def category(tags):
    t = set(tags or ())
    for name, keys in (("Sports", {"Sports"}), ("Crypto", {"Crypto"}), ("AI/Tech", {"AI", "Tech"}),
                       ("Politics", {"Politics", "Elections", "Midterms"}), ("Geopolitics", {"Geopolitics"}),
                       ("Finance/Macro", {"Finance", "Macro", "Stocks", "Commodities", "Companies", "IPO",
                                          "Pre-IPO"}),
                       ("Culture", {"Culture"})):
        if t & keys:
            return name
    return "Other"


def fetch(markets_path, raw):
    raw = Path(raw)
    raw.mkdir(parents=True, exist_ok=True)
    ms = json.loads(Path(markets_path).read_bytes())["data"]
    mp = [m for m in ms if ((m.get("display_attributes") or {}).get("reference") or {}).get("kind") == "polygon"]
    # Gamma by condition id (big payloads; keep the fields we use)
    gpath = raw / "gamma.json.gz"
    if not gpath.exists():
        conds = sorted({m["display_attributes"]["reference"]["condition_id"] for m in mp})
        keep = ("conditionId", "question", "clobTokenIds", "outcomes", "bestBid", "bestAsk", "volume24hr",
                "volume", "feeSchedule", "feeType", "feesEnabled", "closed", "active", "endDate", "negRisk",
                "orderMinSize", "orderPriceMinTickSize", "acceptingOrders", "slug", "liquidityClob")
        out = {}
        for i in range(0, len(conds), 40):
            batch = conds[i:i + 40]
            for closed in ("false", "true"):
                q = [("condition_ids", c) for c in batch if c not in out] + [("limit", 100), ("closed", closed)]
                if len(q) <= 2:
                    break
                for g in get(GAMMA + urllib.parse.urlencode(q)):
                    out[g["conditionId"]] = {k: g.get(k) for k in keep} | {
                        "event_slug": ((g.get("events") or [{}])[0]).get("slug")}
                pace()
            print(f"gamma {i + len(batch)}/{len(conds)} found {len(out)}", flush=True)
        with gzip.open(gpath, "wt") as f:
            json.dump(out, f)
    # Pascal and Polymarket books, interleaved per 50-market batch so both venues are read within ~1 s
    ppath, cpath = raw / "pascal_books.json.gz", raw / "pm_books.json.gz"
    if not ppath.exists() or not cpath.exists():
        pbooks, cbooks = {"_meta": []}, {}
        for i in range(0, len(mp), 50):
            batch = mp[i:i + 50]
            t0 = time.time() * 1000
            r = get(PBOOKS + ",".join(m["symbol"] for m in batch))
            pbooks.update(r["data"]["books"])
            toks = []
            for m in batch:
                ref = m["display_attributes"]["reference"]
                toks += [str(ref["market_outcome_token_id"]), str(ref["reverse_outcome_token_id"])]
            body = json.dumps([{"token_id": t} for t in toks]).encode()
            try:
                cr = get(CBOOKS, data=body)
            except urllib.error.HTTPError as e:
                print("pm books http", e.code, flush=True)
                cr = []
            t1 = time.time() * 1000
            for b in cr or ():
                cbooks[str(b.get("asset_id"))] = {"bids": b.get("bids"), "asks": b.get("asks"),
                                                  "timestamp": b.get("timestamp"),
                                                  "min_order_size": b.get("min_order_size"),
                                                  "tick_size": b.get("tick_size")}
            pbooks["_meta"].append({"i": i, "seq": r.get("seq"), "p_state_ms": r.get("state_time_ms"),
                                    "t0": t0, "t1": t1, "n_pm": len(cr or ())})
            time.sleep(1.0)
            if i % 500 == 0:
                print(f"books {i}/{len(mp)} pm got {len(cbooks)}", flush=True)
        with gzip.open(ppath, "wt") as f:
            json.dump(pbooks, f)
        with gzip.open(cpath, "wt") as f:
            json.dump(cbooks, f)


def best(levels, side):
    """[[p, s], ...] or [{'price','size'}] -> (px, size) best, or (nan, 0)."""
    lv = []
    for x in levels or ():
        p, s = (float(x["price"]), float(x["size"])) if isinstance(x, dict) else (float(x[0]), float(x[1]))
        if s > 0:
            lv.append((p, s))
    if not lv:
        return math.nan, 0.0
    return max(lv) if side == "b" else min(lv)


def fee(rate, p):
    return rate * p * (1 - p)


def pm_rate(g):
    if not g or not g.get("feesEnabled"):
        return 0.0
    fs = g.get("feeSchedule") or {}
    return float(fs.get("rate") or 0.0)


def build(markets_path, raw, now_ms=None):
    raw = Path(raw)
    d = json.loads(Path(markets_path).read_bytes())
    ms = d["data"]
    now_ms = now_ms or float(d.get("state_time_ms") or time.time() * 1000)
    with gzip.open(raw / "gamma.json.gz", "rt") as f:
        gm = json.load(f)
    with gzip.open(raw / "pascal_books.json.gz", "rt") as f:
        pbk = json.load(f)
    with gzip.open(raw / "pm_books.json.gz", "rt") as f:
        cbk = json.load(f)
    rows = []
    for m in ms:
        da = m.get("display_attributes") or {}
        ref = da.get("reference") or {}
        if ref.get("kind") != "polygon":
            continue
        g = gm.get(ref["condition_id"])
        lt, rt = str(ref["market_outcome_token_id"]), str(ref["reverse_outcome_token_id"])
        oriented = None
        if g and g.get("clobTokenIds"):
            ids = json.loads(g["clobTokenIds"])
            oriented = "yes_is_long" if ids and ids[0] == lt else ("no_is_long" if len(ids) > 1 and ids[1] == lt
                                                                   else "token_mismatch")
        pb = pbk.get(m["symbol"]) or {}
        pbid, pbs = best(pb.get("bids"), "b")
        pask, pas = best(pb.get("asks"), "a")
        lb, rb = cbk.get(lt) or {}, cbk.get(rt) or {}
        mlb, mlbs = best(lb.get("bids"), "b")
        mla, mlas = best(lb.get("asks"), "a")
        mrb, mrbs = best(rb.get("bids"), "b")
        mra, mras = best(rb.get("asks"), "a")
        rp, rm = float(m["taker_fee_rate"]), pm_rate(g)
        ert = float(da.get("expected_resolution_time_ms") or 0)
        r = dict(symbol=m["symbol"], event=m["symbol"].split(".")[0], cat=category(da.get("tags")),
                 tags="|".join(da.get("tags") or ()), is_mece=bool(m.get("is_mece")),
                 days=(ert - now_ms) / 86400e3 if ert else math.nan, vol24=vol(m), vol_all=vol(m, "all_time"),
                 trades24=sum(int((m["stats"]["last_24h"]).get(k) or 0) for k in
                              ("taker_buy_trade_count", "taker_sell_trade_count")) if m.get("stats") else 0,
                 long_int=float(m.get("long_interest") or 0), short_int=float(m.get("short_interest") or 0),
                 p_rate=rp, p_rebate=float(m["maker_rebate_share"]), p_bid=pbid, p_bid_sz=pbs, p_ask=pask,
                 p_ask_sz=pas, found=bool(g), pm_closed=(g or {}).get("closed"),
                 pm_accepting=(g or {}).get("acceptingOrders"), orient=oriented, pm_fee_type=(g or {}).get("feeType"),
                 pm_rate=rm, pm_vol24=float((g or {}).get("volume24hr") or 0), pm_l_bid=mlb, pm_l_bid_sz=mlbs,
                 pm_l_ask=mla, pm_l_ask_sz=mlas, pm_r_bid=mrb, pm_r_bid_sz=mrbs, pm_r_ask=mra, pm_r_ask_sz=mras,
                 pm_min=(lb.get("min_order_size")), pm_tick=(lb.get("tick_size")))
        # gaps (positive = locked profit per $1 pair after fees)
        r["gap_LP"] = 1 - (pask + fee(rp, pask) + mra + fee(rm, mra)) if pas and mras else math.nan
        r["sz_LP"] = min(pas, mras) if pas and mras else 0
        r["gap_SP"] = 1 - ((1 - pbid) + fee(rp, pbid) + mla + fee(rm, mla)) if pbs and mlas else math.nan
        r["sz_SP"] = min(pbs, mlas) if pbs and mlas else 0
        rows.append(r)
    return rows


def q(v, ps=(0.1, 0.5, 0.9)):
    v = sorted(x for x in v if x == x)
    if not v:
        return ["-"] * len(ps)
    return [f"{v[min(len(v) - 1, int(p * len(v)))] * 100:.1f}" for p in ps]


def tables(rows, ms_path):
    out = []
    P = out.append
    n = len(rows)
    found = sum(r["found"] for r in rows)
    P(f"Mapped Pascal markets: {n}; found on Gamma: {found}; orientation: {dict(Counter(r['orient'] for r in rows))}; "
      f"PM closed: {sum(bool(r['pm_closed']) for r in rows)}")
    v24 = sum(r["vol24"] for r in rows)
    n1k = sum(r["vol24"] >= 1000 for r in rows)
    P("")
    P(f"**Capacity gate:** mapped markets with Pascal 24 h taker notional >= $1K: **{n1k}** (gate: >= 10); "
      f"total Pascal 24 h taker notional in mapped markets: **${v24:,.0f}** (gate: >= $10K). "
      f"Gate {'PASSES' if n1k >= 10 and v24 >= 10000 else 'FAILS'}.")
    P("")
    P("| category | mapped | with 24h vol | Pascal 24h $ | Pascal all-time $ | PM 24h $ | median days to res | Pascal fee | PM fee types |")
    P("|---|---:|---:|---:|---:|---:|---:|---|---|")
    by = defaultdict(list)
    for r in rows:
        by[r["cat"]].append(r)
    for c, rs in sorted(by.items(), key=lambda kv: -sum(r["vol24"] for r in kv[1])):
        dd = sorted(r["days"] for r in rs if r["days"] == r["days"])
        P(f"| {c} | {len(rs)} | {sum(r['vol24'] > 0 for r in rs)} | {sum(r['vol24'] for r in rs):,.0f} | "
          f"{sum(r['vol_all'] for r in rs):,.0f} | {sum(r['pm_vol24'] for r in rs):,.0f} | "
          f"{dd[len(dd) // 2]:.0f} | {dict(Counter(r['p_rate'] for r in rs))} | "
          f"{dict(Counter((r['pm_fee_type'], r['pm_rate']) for r in rs).most_common(3))} |")
    srt = sorted(rows, key=lambda r: -r["vol24"])
    t20 = sum(r["vol24"] for r in srt[:20])
    P("")
    P(f"Pascal 24 h volume concentration: top 1 {srt[0]['vol24'] / v24:.0%}, top 5 "
      f"{sum(r['vol24'] for r in srt[:5]) / v24:.0%}, top 20 {t20 / v24:.0%}; markets with any 24 h trade: "
      f"{sum(r['vol24'] > 0 for r in rows)}; all-time Pascal notional in mapped markets "
      f"${sum(r['vol_all'] for r in rows):,.0f}.")
    P("")
    P("Top 20 by Pascal 24 h notional:")
    P("")
    P("| symbol | cat | days | P 24h $ | P trades | P bid/ask (size) | PM long bid/ask (size) | PM 24h $ | P fee | PM fee | gap LP / SP (¢) |")
    P("|---|---|---:|---:|---:|---|---|---:|---:|---:|---|")
    for r in srt[:20]:
        P(f"| {r['symbol']} | {r['cat']} | {r['days']:.1f} | {r['vol24']:,.0f} | {r['trades24']} | "
          f"{r['p_bid']:.3f}/{r['p_ask']:.3f} ({r['p_bid_sz']:.0f}/{r['p_ask_sz']:.0f}) | "
          f"{r['pm_l_bid']:.3f}/{r['pm_l_ask']:.3f} ({r['pm_l_bid_sz']:.0f}/{r['pm_l_ask_sz']:.0f}) | "
          f"{r['pm_vol24']:,.0f} | {r['p_rate']} | {r['pm_rate']} | {r['gap_LP'] * 100:.1f} / {r['gap_SP'] * 100:.1f} |")
    P("")
    P("Spread distribution (¢, p10 / p50 / p90), two-sided books only:")
    P("")
    P("| set | n | Pascal | Polymarket (long token) |")
    P("|---|---:|---|---|")
    for name, rs in (("all mapped", rows), ("Pascal 24h vol > 0", [r for r in rows if r["vol24"] > 0])):
        ps = [r["p_ask"] - r["p_bid"] for r in rs if r["p_bid_sz"] and r["p_ask_sz"]]
        ms = [r["pm_l_ask"] - r["pm_l_bid"] for r in rs if r["pm_l_bid_sz"] and r["pm_l_ask_sz"]]
        P(f"| {name} | {len(rs)} | {' / '.join(q(ps))} (n={len(ps)}) | {' / '.join(q(ms))} (n={len(ms)}) |")
    P("")
    P("Gap now (after both taker fees, top of book). Displayed shares = min of the two top-level sizes; "
      "$ = gap x shares:")
    P("")
    P("| direction | > 1¢ n | > 1¢ shares | > 1¢ $ | > 2¢ n | > 2¢ shares | > 2¢ $ | > 4¢ n | > 4¢ shares | > 4¢ $ | best gap ¢ |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for dr in ("LP", "SP"):
        cells = []
        live = [r for r in rows if r["found"] and not r["pm_closed"]]
        for cut in GAP_CUTS:
            rs = [r for r in live if r[f"gap_{dr}"] == r[f"gap_{dr}"] and r[f"gap_{dr}"] > cut]
            cells += [str(len(rs)), f"{sum(r[f'sz_{dr}'] for r in rs):,.0f}",
                      f"{sum(r[f'sz_{dr}'] * r[f'gap_{dr}'] for r in rs):,.2f}"]
        g = max((r[f"gap_{dr}"] for r in live if r[f"gap_{dr}"] == r[f"gap_{dr}"]), default=math.nan)
        P(f"| {dr} | " + " | ".join(cells) + f" | {g * 100:.1f} |")
    P("")
    gl = sorted((r for r in rows if r["found"] and not r["pm_closed"]),
                key=lambda r: -max(x for x in (r["gap_LP"], r["gap_SP"], -9) if x == x))[:15]
    P("Largest gaps now:")
    P("")
    P("| symbol | cat | days | dir | gap ¢ | shares | P bid/ask (size) | PM L bid/ask (size) | PM R ask (size) | P 24h $ |")
    P("|---|---|---:|---|---:|---:|---|---|---|---:|")
    for r in gl:
        dr = "LP" if (r["gap_LP"] if r["gap_LP"] == r["gap_LP"] else -9) >= (
            r["gap_SP"] if r["gap_SP"] == r["gap_SP"] else -9) else "SP"
        P(f"| {r['symbol']} | {r['cat']} | {r['days']:.1f} | {dr} | {r[f'gap_{dr}'] * 100:.1f} | {r[f'sz_{dr}']:.0f} | "
          f"{r['p_bid']:.3f}/{r['p_ask']:.3f} ({r['p_bid_sz']:.0f}/{r['p_ask_sz']:.0f}) | "
          f"{r['pm_l_bid']:.3f}/{r['pm_l_ask']:.3f} ({r['pm_l_bid_sz']:.0f}/{r['pm_l_ask_sz']:.0f}) | "
          f"{r['pm_r_ask']:.3f} ({r['pm_r_ask_sz']:.0f}) | {r['vol24']:,.0f} |")
    # MECE
    ms = json.loads(Path(ms_path).read_bytes())["data"]
    ev = defaultdict(list)
    for m in ms:
        if m.get("is_mece"):
            ev[m["symbol"].split(".")[0]].append(m["symbol"])
    rmap = {r["symbol"]: r for r in rows}
    allp = {m["symbol"]: m for m in ms}
    res = []
    for e, syms in ev.items():
        if not all(s in rmap for s in syms):
            continue
        rs = [rmap[s] for s in syms]
        okp = all(r["p_ask_sz"] for r in rs)
        okm = all(r["pm_l_ask_sz"] for r in rs)
        cp = sum(r["p_ask"] + fee(r["p_rate"], r["p_ask"]) for r in rs) if okp else math.nan
        cm = sum(r["pm_l_ask"] + fee(r["pm_rate"], r["pm_l_ask"]) for r in rs) if okm else math.nan
        okps = all(r["p_bid_sz"] for r in rs)
        cps = sum((1 - r["p_bid"]) + fee(r["p_rate"], r["p_bid"]) for r in rs) - (len(rs) - 1) if okps else math.nan
        res.append(dict(event=e, n=len(rs), cp=cp, cm=cm, cps=cps, cat=rs[0]["cat"],
                        szp=min((r["p_ask_sz"] for r in rs), default=0), v=sum(r["vol24"] for r in rs),
                        days=rs[0]["days"]))
    P("")
    nm = len(ev)
    P(f"MECE events on Pascal: {nm}; with every outcome mapped: {len(res)}; fully two-sided on Pascal asks: "
      f"{sum(r['cp'] == r['cp'] for r in res)}.")
    P("")
    P("| basket | events priced | cost < $1 - 1¢ | < $1 - 2¢ | < $1 - 4¢ | median cost $ | min cost $ |")
    P("|---|---:|---:|---:|---:|---:|---:|")
    for name, k in (("buy every outcome, Pascal", "cp"), ("sell every outcome (buy all No), Pascal, per $1", "cps"),
                    ("buy every outcome, Polymarket", "cm")):
        v = sorted(r[k] for r in res if r[k] == r[k])
        P(f"| {name} | {len(v)} | {sum(x < 0.99 for x in v)} | {sum(x < 0.98 for x in v)} | "
          f"{sum(x < 0.96 for x in v)} | {v[len(v) // 2] if v else math.nan:.3f} | {v[0] if v else math.nan:.3f} |")
    cheap = sorted((r for r in res if r["cp"] == r["cp"] and r["cp"] < 0.99), key=lambda r: r["cp"])[:10]
    if cheap:
        P("")
        P("Cheapest Pascal buy-all baskets: " + "; ".join(
            f"{r['event']} n={r['n']} ${r['cp']:.3f} (min ask size {r['szp']:.0f}, PM ${r['cm']:.3f}, 24h ${r['v']:,.0f})"
            for r in cheap))
    _ = allp
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["fetch", "table", "all"])
    p.add_argument("--markets", default=str(Path.home() / "pascal-data/raw/snap/markets_20261127.json"))
    p.add_argument("--raw", default=str(Path.home() / "pascal-data/raw/census"))
    p.add_argument("--csv", default="reports/tokyo/pascal_census_20261127.csv")
    p.add_argument("--md", default="reports/tokyo/pascal_census_20261127.md")
    a = p.parse_args()
    if a.cmd in ("fetch", "all"):
        fetch(a.markets, a.raw)
    if a.cmd in ("table", "all"):
        rows = build(a.markets, a.raw)
        Path(a.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        md = tables(rows, a.markets)
        Path(a.md).write_text(md + "\n")
        print(md)


if __name__ == "__main__":
    main()
