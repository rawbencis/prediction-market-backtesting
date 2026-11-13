"""Dissect one Polymarket wallet (default: OhioAI) trading BTC up/down markets.

Uses the Polymarket Data API v2 (cursor pagination, snake_case rows; v1 returns 410).
probe    print raw samples from /v2/activity, /v2/trades, /v2/user-pnl (schema check)
fetch    full /v2/activity history (all types incl. rebates) -> act.jsonl
         full /v2/trades taker_only=true history -> taker.jsonl (maker = activity trades not in it)
         /v2/user-pnl interval=max fidelity=1d -> pnl.json (cumulative pnl, fees, maker rebates, volume)
settle   winners for every up/down market traded (stwf2 settlements first, then Gamma) -> winners.csv
report   per-market books, PnL split (pair lock / direction / sells / merges), monthly, timing, prices,
         and for Jun-Jul markets: join with the stwf2 top-of-book cache (block-time lag, maker/taker, markouts)
"""

import argparse
import csv
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

WALLET = "0x129246beaac356e104c397f0985c11b7469be5a6"
DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"
SLUG = re.compile(r"^(btc|eth|sol|xrp|doge|bnb)-updown-(5m|15m|1h|4h)-(\d+)$")
LEN = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def get(url, params, tries=6):
    u = url + "?" + urllib.parse.urlencode(params, doseq=True)
    for k in range(tries):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "research-wallet-dissect"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and k < tries - 1:
                time.sleep(min(2**k, 30))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if k < tries - 1:
                time.sleep(min(2**k, 30))
                continue
            raise


def rows_of(x):
    return x["data"] if isinstance(x, dict) and "data" in x else x


V2 = dict(condition_id="conditionId", usdc_size="usdcSize", transaction_hash="transactionHash", token_id="asset", outcome_index="outcomeIndex", proxy_wallet="proxyWallet", event_slug="eventSlug")


def norm(r):
    return {V2.get(k, k): v for k, v in r.items()}


def key(r):
    return (r.get("transactionHash"), r.get("asset"), r.get("type", "TRADE"), r.get("side"), str(r.get("size")), str(r.get("price")), r.get("timestamp"))


def pages(path, params, sleep):
    """follow pagination.next_cursor, re-sending identical filters."""
    cur = None
    while True:
        p = dict(params)
        if cur:
            p["cursor"] = cur
        x = get(DATA + path, p)
        yield [norm(r) for r in rows_of(x)]
        pg = x.get("pagination", {}) if isinstance(x, dict) else {}
        cur = pg.get("next_cursor")
        if not cur or not pg.get("has_more", True):
            return
        time.sleep(sleep)


# ---------------------------------------------------------------- probe / fetch


def cmd_probe(a):
    for path, p in (
        ("/activity", dict(user=a.wallet, limit=3)),
        ("/trades", dict(user=a.wallet, limit=3, taker_only="false")),
        ("/trades", dict(user=a.wallet, limit=3, taker_only="true")),
        ("/user-pnl", dict(user=a.wallet, interval="1w", fidelity="1d")),
    ):
        try:
            x = get(DATA + path, p)
            r = rows_of(x)
            print(f"== {path} {p}: keys {sorted(x) if isinstance(x, dict) else type(x)}")
            print(json.dumps(r[:2] if isinstance(r, list) else r, indent=1)[:2500])
        except Exception as e:  # noqa: BLE001
            print(f"== {path} {p}: ERROR {e!r}")


def dump(f, path, params, sleep, label):
    n = 0
    tmp = f.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for rows in pages(path, params, sleep):
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            n += len(rows)
            if rows:
                day = datetime.fromtimestamp(int(rows[-1]["timestamp"]), timezone.utc).isoformat()[:16]
                print(f"{label}: {n} rows, at {day}", flush=True)
    tmp.replace(f)
    print(f"{label}: {n} rows -> {f}")


def cmd_fetch(a):
    a.out.mkdir(parents=True, exist_ok=True)
    types = "TRADE,SPLIT,MERGE,REDEEM,CONVERSION,REWARD,YIELD,MAKER_REBATE,TAKER_REBATE,REFERRAL_REWARD,DIRECTIONAL_SPLIT,DIRECTIONAL_MERGE"
    dump(a.out / "act.jsonl", "/activity", dict(user=a.wallet, limit=1000, start=a.since or 1, type=types, sort_direction="ASC"), a.sleep, "activity")
    cmd_taker(a)
    x = get(DATA + "/user-pnl", dict(user=a.wallet, interval="max", fidelity="1d"))
    (a.out / "pnl.json").write_text(json.dumps(x))
    print(f"user-pnl: {len(rows_of(x).get('points', []))} daily points -> {a.out / 'pnl.json'}")


def cmd_taker(a):
    dump(a.out / "taker.jsonl", "/trades", dict(user=a.wallet, limit=1000, start=a.since or 1, taker_only="true"), a.sleep, "taker trades")


# ---------------------------------------------------------------- settle


def cmd_settle(a):
    acts = [json.loads(x) for x in (a.out / "act.jsonl").open()]
    slugs = sorted({r["slug"] for r in acts if SLUG.match(r.get("slug") or "")})
    f = a.out / "winners.csv"
    have = {}
    if f.exists():
        have = {r["slug"]: r["winner"] for r in csv.DictReader(f.open())}
    if a.cache and (a.cache / "settlements.csv").exists():
        for r in csv.DictReader((a.cache / "settlements.csv").open()):
            have.setdefault(r["slug"], r["winner"])
    todo = [s for s in slugs if s not in have]
    print(f"{len(slugs)} up/down markets, {len(todo)} to resolve via Gamma", flush=True)

    def parse(rows, want, new):
        for m in rows:
            try:
                names = [x.upper() for x in json.loads(m["outcomes"])]
                px = [float(v) for v in json.loads(m["outcomePrices"])]
            except (KeyError, TypeError, ValueError):
                continue
            if m.get("slug") in want and m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(px) >= 0.99 and min(px) <= 0.01:
                new[m["slug"]] = names[px.index(max(px))]

    for i in range(0, len(todo), 50):
        b = todo[i : i + 50]
        new = {}
        try:
            parse(get(GAMMA + "/markets", [("closed", "true"), ("limit", 500)] + [("slug", s) for s in b]), set(b), new)
            for s in [x for x in b if x not in new]:
                parse(get(GAMMA + "/markets", [("slug", s)]), {s}, new)
        except Exception as e:  # noqa: BLE001
            print("gamma failed", i, repr(e), flush=True)
        have.update(new)
        if i % 1000 == 0:
            print(f"{i + len(b)}/{len(todo)}", flush=True)
        time.sleep(a.sleep)
    with f.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["slug", "winner"])
        w.writerows(sorted((k, v) for k, v in have.items() if k in set(slugs)))
    print(f"winners for {sum(s in have for s in slugs)}/{len(slugs)} markets -> {f}")


# ---------------------------------------------------------------- books


def side_of(r):
    o = (r.get("outcome") or "").upper()
    if o in ("UP", "DOWN"):
        return 0 if o == "UP" else 1
    oi = r.get("outcomeIndex")
    return int(oi) if oi in (0, 1, "0", "1") else None


def books(acts, winners):
    """per-market reconstruction. returns dict slug -> stats."""
    by = defaultdict(list)
    for r in acts:
        m = SLUG.match(r.get("slug") or "")
        if m:
            by[r["slug"]].append(r)
    out = {}
    for slug, rs in by.items():
        asset, kind, ep = SLUG.match(slug).groups()
        ep = int(ep)
        bq, bc, sq, sc = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]
        split, merge, splitq, mergeq, redeem, nb, ns = 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0
        fills = []
        for r in rs:
            t, sz, usd = r.get("type", "TRADE"), float(r.get("size") or 0), float(r.get("usdcSize") or 0)
            if t == "TRADE":
                k, px = side_of(r), float(r.get("price") or 0)
                if k is None:
                    continue
                usd = usd or sz * px
                if r.get("side") == "BUY":
                    bq[k] += sz
                    bc[k] += usd
                    nb += 1
                    fills.append((int(r["timestamp"]) - ep, k, px, sz))
                else:
                    sq[k] += sz
                    sc[k] += usd
                    ns += 1
            elif t == "SPLIT":
                split += usd
                splitq += sz
            elif t == "MERGE":
                merge += usd
                mergeq += sz
            elif t == "REDEEM":
                redeem += usd
        q = [bq[i] - sq[i] + splitq - mergeq for i in (0, 1)]
        cash = sc[0] + sc[1] + merge - bc[0] - bc[1] - split
        W = winners.get(slug)
        w = None if W is None else (0 if W == "UP" else 1)
        pnl = None if w is None else cash + q[w]
        pq = min(bq)
        avg = [bc[i] / bq[i] if bq[i] else np.nan for i in (0, 1)]
        pair = pq * (1 - avg[0] - avg[1]) if pq > 0 else 0.0
        out[slug] = dict(
            asset=asset, kind=kind, ep=ep, day=datetime.fromtimestamp(ep, timezone.utc).date().isoformat(),
            bq=bq, bc=bc, sq=sq, sc=sc, split=split, merge=merge, redeem=redeem, nb=nb, ns=ns, q=q,
            pnl=pnl, w=w, pair_q=pq, pair_cost=(avg[0] + avg[1]) if pq > 0 else np.nan, pair_lock=pair,
            vol=bc[0] + bc[1] + sc[0] + sc[1], fills=fills,
        )
    return out


# ---------------------------------------------------------------- book join (stwf2 cache)


def book_join(B, cache, lags=range(0, 11)):
    """for BTC 5m fills in markets present in the npz cache: match fill price to bid/ask at t-lag."""
    if cache is None:
        return None
    idx = {}
    for d in sorted(cache.glob("????-??-??")):
        for f in d.glob("[0-9]*.npz"):
            idx[int(f.stem)] = f
    M = {s: b for s, b in B.items() if b["kind"] == "5m" and b["asset"] == "btc" and b["ep"] in idx}
    hit_b = np.zeros(len(lags))
    hit_a = np.zeros(len(lags))
    n = 0
    rec = []  # (lag-free) per fill: px, bid@t-L, ask@t-L, mid@t+30, mid@t+60, win, t_rel
    for s, b in M.items():
        if not b["fills"]:
            continue
        with np.load(idx[b["ep"]]) as z:
            t, bid, ask = z["t"], z["bid"], z["ask"]
        if len(t) < 2:
            continue
        for tr, k, px, sz in b["fills"]:
            p = round(px * 1000)
            n += 1
            for li, L in enumerate(lags):
                i = np.searchsorted(t, (tr - L) * 1_000_000 + 999_999, "right") - 1
                if i >= 0:
                    hit_b[li] += bid[i, k] == p
                    hit_a[li] += ask[i, k] == p
            rec.append((s, tr, k, p, sz, b["w"]))
    if not n:
        return dict(markets=0)
    L = int(np.argmax(hit_b + hit_a))
    out = dict(markets=len(M), fills=n, lag_s=list(lags), frac_at_bid=(hit_b / n).round(3).tolist(), frac_at_ask=(hit_a / n).round(3).tolist(), best_lag_s=L)
    # classify + markouts at best lag
    cls = Counter()
    mk = defaultdict(list)
    for s, tr, k, p, sz, w in rec:
        with np.load(idx[M[s]["ep"]]) as z:
            t, bid, ask = z["t"], z["bid"].astype(int), z["ask"].astype(int)
        i = np.searchsorted(t, (tr - L) * 1_000_000 + 999_999, "right") - 1
        if i < 0 or bid[i, k] < 0:
            continue
        bi, ai = bid[i, k], ask[i, k]
        c = "at_bid" if p == bi else "at_ask" if p == ai else "below_bid" if p < bi else "above_ask" if p > ai else "inside"
        cls[c] += 1
        mid0 = (bi + ai) / 2
        row = dict(edge_vs_mid=mid0 - p, spread=ai - bi)
        for h in (5, 30, 60):
            j = np.searchsorted(t, (tr - L + h) * 1_000_000, "right") - 1
            if j >= 0 and bid[j, k] >= 0:
                row[f"mid+{h}s"] = (bid[j, k] + ask[j, k]) / 2 - p
        if w is not None:
            row["settle"] = 1000 * (w == k) - p
        for kk, v in row.items():
            mk[(c, kk)].append((v, sz))
    tab = {}
    for (c, kk), v in mk.items():
        v = np.array(v)
        tab.setdefault(c, {})[kk] = round(float((v[:, 0] * v[:, 1]).sum() / v[:, 1].sum()), 2)
    out["class_counts"] = dict(cls)
    out["markout_milli_per_share_size_weighted"] = tab
    return out


# ---------------------------------------------------------------- report


def q(x, ps=(10, 25, 50, 75, 90)):
    x = np.asarray([v for v in x if v == v], float)
    return [round(float(np.percentile(x, p)), 3) for p in ps] if len(x) else []


def in_range(acts, a):
    lo = int(datetime.fromisoformat(a.date_from).replace(tzinfo=timezone.utc).timestamp()) if a.date_from else 0
    hi = int(datetime.fromisoformat(a.date_to).replace(tzinfo=timezone.utc).timestamp()) if a.date_to else 1 << 40
    return [r for r in acts if lo <= int(r["timestamp"]) < hi]


def cmd_report(a):
    acts = in_range([json.loads(x) for x in (a.out / "act.jsonl").open()], a)
    wf = a.out / "winners.csv"
    winners = {r["slug"]: r["winner"] for r in csv.DictReader(wf.open())} if wf.exists() else {}
    rep = {}
    ty = Counter(r.get("type", "TRADE") for r in acts)
    ts = [int(r["timestamp"]) for r in acts]
    rep["rows_by_type"] = dict(ty)
    rep["range"] = [datetime.fromtimestamp(min(ts), timezone.utc).isoformat(), datetime.fromtimestamp(max(ts), timezone.utc).isoformat()]
    usd_by_type = defaultdict(float)
    for r in acts:
        usd_by_type[r.get("type", "TRADE")] += float(r.get("usdcSize") or 0)
    rep["usdc_by_type"] = {k: round(v, 2) for k, v in usd_by_type.items()}
    other = Counter((r.get("slug") or "?").rsplit("-", 1)[0] for r in acts if r.get("type") == "TRADE" and not SLUG.match(r.get("slug") or ""))
    rep["non_updown_trade_rows_top"] = other.most_common(10)
    B = books(acts, winners)
    kinds = Counter((b["asset"], b["kind"]) for b in B.values())
    rep["markets_by_asset_kind"] = {f"{k[0]}-{k[1]}": v for k, v in kinds.most_common()}
    S = [b for b in B.values() if b["pnl"] is not None]
    rep["settled_markets"] = len(S)
    rep["unsettled_markets"] = len(B) - len(S)
    tot_pnl = sum(b["pnl"] for b in S)
    tot_vol = sum(b["vol"] for b in S)
    rep["pnl_usd"] = round(tot_pnl, 2)
    rep["volume_usd"] = round(tot_vol, 2)
    rep["c_per_usd"] = round(100 * tot_pnl / max(tot_vol, 1), 3)
    # monthly
    mon = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for b in S:
        m = mon[b["day"][:7]]
        m[0] += 1
        m[1] += b["vol"]
        m[2] += b["pnl"]
        m[3] += b["pair_lock"]
    reb = defaultdict(float)
    for r in acts:
        if r.get("type") not in ("TRADE", "SPLIT", "MERGE", "REDEEM"):
            reb[datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).strftime("%Y-%m")] += float(r.get("usdcSize") or 0)
    rep["monthly"] = {k: dict(markets=v[0], vol=round(v[1]), pnl=round(v[2], 2), c_per_usd=round(100 * v[2] / max(v[1], 1), 3), pair_lock=round(v[3], 2), other_income=round(reb.get(k, 0), 2)) for k, v in sorted(mon.items())}
    # daily distribution
    day = defaultdict(float)
    for b in S:
        day[b["day"]] += b["pnl"]
    dv = np.array(list(day.values()))
    rep["daily_pnl"] = dict(days=len(dv), mean=round(float(dv.mean()), 2), sd=round(float(dv.std()), 2), pct_positive=round(float((dv > 0).mean()), 3), worst=round(float(dv.min()), 2), best=round(float(dv.max()), 2)) if len(dv) else {}
    # structure, per asset-kind
    st = {}
    for ak in rep["markets_by_asset_kind"]:
        asset, kind = ak.split("-")
        X = [b for b in S if b["asset"] == asset and b["kind"] == kind]
        if len(X) < 20:
            continue
        both = [b for b in X if min(b["bq"]) > 0]
        bq = np.array([sum(b["bq"]) for b in X])
        pq = np.array([b["pair_q"] for b in X])
        pnl = np.array([b["pnl"] for b in X])
        vol = np.array([b["vol"] for b in X])
        lock = np.array([b["pair_lock"] for b in X])
        st[ak] = dict(
            markets=len(X),
            pnl=round(float(pnl.sum()), 2),
            c_per_usd=round(100 * pnl.sum() / max(vol.sum(), 1), 3),
            pct_markets_positive=round(float((pnl > 0).mean()), 3),
            pnl_per_market_q=q(pnl),
            vol_per_market_q=q(vol),
            pct_both_sides=round(len(both) / len(X), 3),
            paired_share_of_bought_qty=round(float(2 * pq.sum() / max(bq.sum(), 1)), 3),
            pair_cost_q_paired_markets=q([b["pair_cost"] for b in both]),
            pair_lock_usd=round(float(lock.sum()), 2),
            direction_and_rest_usd=round(float(pnl.sum() - lock.sum()), 2),
            sell_share_of_volume=round(sum(sum(b["sc"]) for b in X) / max(vol.sum(), 1), 3),
            merge_usd=round(sum(b["merge"] for b in X), 2),
            split_usd=round(sum(b["split"] for b in X), 2),
            buys_per_market_q=q([b["nb"] for b in X]),
            pnl_one_sided_markets=round(sum(b["pnl"] for b in X if min(b["bq"]) == 0), 2),
            pnl_two_sided_markets=round(sum(b["pnl"] for b in both), 2),
        )
        L = LEN[kind]
        F = [f for b in X for f in b["fills"]]
        if F:
            tr = np.array([f[0] for f in F])
            px = np.array([f[2] for f in F])
            sz = np.array([f[3] for f in F])
            edges = np.linspace(0, L, 11)
            h = np.histogram(np.clip(tr, 0, L - 1), edges, weights=sz * px)[0]
            st[ak]["buy_usd_share_by_tenth_of_window"] = (h / h.sum()).round(3).tolist()
            st[ak]["buy_fill_before_open_share"] = round(float((tr < 0).mean()), 3)
            hp = np.histogram(px, np.linspace(0, 1, 11), weights=sz)[0]
            st[ak]["buy_shares_by_price_decile"] = (hp / hp.sum()).round(3).tolist()
            st[ak]["fill_size_shares_q"] = q(sz)
            gaps = []
            for b in both:
                t0 = [min(f[0] for f in b["fills"] if f[1] == k) for k in (0, 1)]
                gaps.append(abs(t0[0] - t0[1]))
            st[ak]["first_up_vs_first_down_gap_s_q"] = q(gaps)
    rep["structure"] = st
    rep["book_join_btc5m"] = book_join(B, a.cache) if a.cache else None
    # maker vs taker (full taker history)
    tf = a.out / "taker.jsonl"
    if tf.exists():
        tk = [json.loads(x) for x in tf.open()]
        tkeys = Counter((r.get("transactionHash"), r.get("asset"), r.get("side"), round(float(r.get("size") or 0), 4)) for r in tk)
        tr_ = [r for r in acts if r.get("type") == "TRADE"]
        it = []
        for r in tr_:
            kk = (r.get("transactionHash"), r.get("asset"), r.get("side"), round(float(r.get("size") or 0), 4))
            hit = tkeys[kk] > 0
            if hit:
                tkeys[kk] -= 1
            it.append(hit)
        it = np.array(it, bool)
        usd = np.array([float(r.get("usdcSize") or 0) or float(r.get("size") or 0) * float(r.get("price") or 0) for r in tr_])
        mon = np.array([datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).strftime("%Y-%m") for r in tr_])
        rep["maker_taker"] = dict(
            taker_rows=len(tk), trade_rows=len(tr_), taker_fill_share=round(float(it.mean()), 3) if len(it) else None,
            taker_usd_share=round(float(usd[it].sum() / max(usd.sum(), 1)), 3),
            unmatched_taker_rows=int(sum(v for v in tkeys.values() if v > 0)),
            taker_usd_share_by_month={m: round(float(usd[(mon == m) & it].sum() / max(usd[mon == m].sum(), 1)), 3) for m in sorted(set(mon))},
        )
    # fee-implied role: usdc_size includes the taker fee (0.07*p*(1-p) per share); makers pay none
    tr_ = [r for r in acts if r.get("type") == "TRADE" and r.get("side") == "BUY"]
    if tr_:
        sz = np.array([float(r.get("size") or 0) for r in tr_])
        px = np.array([float(r.get("price") or 0) for r in tr_])
        usd = np.array([float(r.get("usdcSize") or 0) for r in tr_])
        fee = usd - sz * px
        exp = 0.07 * px * (1 - px) * sz
        tk = fee > 0.5 * exp
        mon = np.array([datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).strftime("%Y-%m") for r in tr_])
        rep["fee_implied_buys"] = dict(
            buys=len(tr_), taker_share=round(float(tk.mean()), 3), taker_usd_share=round(float(usd[tk].sum() / max(usd.sum(), 1)), 3),
            fees_paid_usd=round(float(fee[tk].sum()), 2), fee_over_expected_q=q(fee[exp > 0] / exp[exp > 0]),
            taker_usd_share_by_month={m: round(float(usd[(mon == m) & tk].sum() / max(usd[mon == m].sum(), 1)), 3) for m in sorted(set(mon))},
        )
    # official cumulative pnl series
    pf = a.out / "pnl.json"
    if pf.exists():
        pts = rows_of(json.loads(pf.read_text())).get("points", [])
        if pts:
            by = {}
            for pt in pts:
                by[datetime.fromtimestamp(int(pt["timestamp"]), timezone.utc).strftime("%Y-%m")] = pt  # last point of month
            prev = {}
            mo = {}
            for m, pt in sorted(by.items()):
                row = {}
                for f in ("realized_pnl", "economic_pnl", "volume_usdc", "trade_count", "fees", "maker_rebate", "taker_rebate", "reward_income"):
                    v = pt.get(f)
                    if v is not None:
                        row[f] = round(float(v) - float(prev.get(f, 0) or 0), 2)
                mo[m] = row
                prev = {k: v for k, v in pt.items() if v is not None}
            rep["official_monthly"] = mo
            rep["official_cumulative_last"] = {k: v for k, v in pts[-1].items() if k not in ("source_block",)}
    (a.out / "report.json").write_text(json.dumps(rep, indent=1, default=float))
    print(json.dumps(rep, indent=1, default=float))


# ---------------------------------------------------------------- decision rule vs Binance 1s spot

PHI = np.vectorize(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))


def klines(day, d, sym="BTCUSDT"):
    """Binance spot 1s klines for one UTC day -> (sec array, close array); cached as .npy."""
    f = d / f"{sym}-{day}.npy"
    if not f.exists():
        import io
        import zipfile

        url = f"https://data.binance.vision/data/spot/daily/klines/{sym}/1s/{sym}-1s-{day}.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "research-wallet-dissect"})
        with urllib.request.urlopen(req, timeout=120) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        raw = np.genfromtxt(z.open(z.namelist()[0]), delimiter=",", usecols=(0, 4))
        t = raw[:, 0]
        t = np.where(t > 1e14, t / 1e6, np.where(t > 1e11, t / 1e3, t)).astype(np.int64)
        np.save(f, np.stack([t, raw[:, 1]], 1))
    x = np.load(f)
    return x[:, 0].astype(np.int64), x[:, 1]


def cell_table(rows, keys, edges, names):
    out = {}
    for k, e, nm in zip(keys, edges, names):
        v = np.array([r[k] for r in rows], float)
        b = np.digitize(v, e) - 1
        for i in range(len(e) - 1):
            sel = [r for r, bb in zip(rows, b) if bb == i]
            if len(sel) < 30:
                continue
            usd = sum(r["usd"] for r in sel)
            out[f"{nm} {e[i]:g}..{e[i + 1]:g}"] = dict(
                fills=len(sel), usd=round(usd), win=round(float(np.mean([r["win"] for r in sel])), 3),
                avg_px=round(float(np.mean([r["px"] for r in sel])), 3), avg_fair=round(float(np.mean([r["fair"] for r in sel])), 3),
                net_c_per_usd=round(100 * sum(r["pnl"] for r in sel) / max(usd, 1e-9), 2),
            )
    return out


def cmd_rule(a):
    acts = in_range([json.loads(x) for x in (a.out / "act.jsonl").open()], a)
    winners = {r["slug"]: r["winner"] for r in csv.DictReader((a.out / "winners.csv").open())}
    kd = a.klines
    kd.mkdir(parents=True, exist_ok=True)
    fills = []
    for r in acts:
        m = SLUG.match(r.get("slug") or "")
        if not m or m.group(1) != "btc" or r.get("type") != "TRADE" or r.get("side") != "BUY":
            continue
        k, W = side_of(r), winners.get(r["slug"])
        if k is None or W is None:
            continue
        sz, px, usd = float(r["size"]), float(r["price"]), float(r.get("usdcSize") or 0)
        fee = usd - sz * px
        fills.append(dict(ep=int(m.group(3)), L=LEN[m.group(2)], kind=m.group(2), ts=int(r["timestamp"]), k=k, px=px, sz=sz, usd=usd,
                          taker=fee > 0.5 * 0.07 * px * (1 - px) * sz, win=int((W == "UP") == (k == 0)), pnl=sz * int((W == "UP") == (k == 0)) - usd))
    days = sorted({datetime.fromtimestamp(f["ep"], timezone.utc).date().isoformat() for f in fills} | {datetime.fromtimestamp(f["ts"], timezone.utc).date().isoformat() for f in fills})
    T, C = [], []
    for i, d in enumerate(days):
        try:
            t, c = klines(d, kd)
            T.append(t)
            C.append(c)
        except Exception as e:  # noqa: BLE001
            print("klines failed", d, repr(e), flush=True)
        if i % 10 == 0:
            print(f"klines {i + 1}/{len(days)}", flush=True)
    T, C = np.concatenate(T), np.concatenate(C)
    o = np.argsort(T)
    T, C = T[o], C[o]
    LR = np.r_[0, np.diff(np.log(C))]

    def px_at(sec):
        i = np.searchsorted(T, sec, "right") - 1
        return np.where(i >= 0, C[np.maximum(i, 0)], np.nan)

    rep = dict(range=[a.date_from, a.date_to], lag_s=a.lag)
    ep = np.array([f["ep"] for f in fills])
    ts = np.array([f["ts"] for f in fills])
    Ls = np.array([f["L"] for f in fills])
    k = np.array([f["k"] for f in fills])
    p0 = px_at(ep - 1)
    sens = {}
    for lag in range(0, 7):
        td = ts - lag
        cur = px_at(td)
        i1 = np.searchsorted(T, td, "right")
        sig = np.array([LR[max(j - 300, 0):j].std() if j > 30 else np.nan for j in i1])
        tl = np.maximum(ep + Ls - td, 1)
        z = np.log(cur / p0) / (sig * np.sqrt(tl))
        fair = PHI(np.nan_to_num(z))
        fair = np.where(k == 0, fair, 1 - fair)
        px = np.array([f["px"] for f in fills])
        tk = np.array([f["taker"] for f in fills])
        sens[lag] = dict(taker_frac_fair_gt_px=round(float((fair[tk] > px[tk]).mean()), 3) if tk.any() else None, taker_mean_fair_minus_px=round(float(np.nanmean(fair[tk] - px[tk])), 4) if tk.any() else None)
        if lag == a.lag:
            m5 = np.log(cur / px_at(td - 5)) * 1e4
            for f, zz, fa, tlv, mm in zip(fills, z, fair, tl, m5):
                f["z_side"] = float(zz if f["k"] == 0 else -zz) if zz == zz else np.nan
                f["fair"] = float(fa)
                f["edge"] = float(fa) - f["px"]
                f["tleft"] = float(tlv)
                f["mom5_side"] = float(mm if f["k"] == 0 else -mm) if mm == mm else np.nan
    rep["lag_sensitivity"] = sens
    for role in ("taker", "maker"):
        R = [f for f in fills if f["taker"] == (role == "taker") and f.get("fair") == f.get("fair")]
        if not R:
            continue
        usd = sum(f["usd"] for f in R)
        rep[role] = dict(
            fills=len(R), usd=round(usd), pnl=round(sum(f["pnl"] for f in R), 2), net_c_per_usd=round(100 * sum(f["pnl"] for f in R) / usd, 2),
            kinds=dict(Counter(f["kind"] for f in R)),
            share_mom5_toward_side=round(float(np.mean([f["mom5_side"] > 0 for f in R if f["mom5_side"] == f["mom5_side"]])), 3),
            z_side_q=q([f["z_side"] for f in R]), edge_q=q([f["edge"] for f in R]), tleft_q=q([f["tleft"] for f in R]), px_q=q([f["px"] for f in R]),
            by_tleft=cell_table(R, ["tleft"], [[0, 5, 10, 20, 30, 60, 120, 300, 900]], ["secs_left"]),
            by_price=cell_table(R, ["px"], [[0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0]], ["px"]),
            by_edge=cell_table(R, ["edge"], [[-1, -0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2, 0.3, 1]], ["fair-px"]),
            by_z_side=cell_table(R, ["z_side"], [[-9, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 99]], ["z_side"]),
            by_mom5=cell_table(R, ["mom5_side"], [[-1e9, -5, -2, 0, 2, 5, 10, 1e9]], ["mom5_bps"]),
        )
    # model sanity: is our spot fair calibrated on these fills?
    allf = [f for f in fills if f.get("fair") == f.get("fair")]
    if allf:
        fa = np.array([f["fair"] for f in allf])
        w = np.array([f["win"] for f in allf])
        b = np.digitize(fa, np.linspace(0, 1, 11)) - 1
        rep["fair_calibration"] = {f"{i / 10:.1f}": [int((b == i).sum()), round(float(w[b == i].mean()), 3)] for i in range(10) if (b == i).sum() >= 30}
    (a.out / f"rule_{a.date_from or 'all'}.json").write_text(json.dumps(rep, indent=1, default=float))
    print(json.dumps(rep, indent=1, default=float))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "fetch", "taker", "settle", "report", "rule"):
        p = sp.add_parser(name)
        p.add_argument("--wallet", default=WALLET)
        p.add_argument("--out", type=Path, default=Path.home() / "ohioai")
        p.add_argument("--cache", type=Path, default=None)
        p.add_argument("--since", type=int, default=0)
        p.add_argument("--sleep", type=float, default=0.25)
        p.add_argument("--from", dest="date_from", default="")
        p.add_argument("--to", dest="date_to", default="")
        p.add_argument("--klines", type=Path, default=Path.home() / "binance_1s")
        p.add_argument("--lag", type=int, default=3)
    a = ap.parse_args()
    {"probe": cmd_probe, "fetch": cmd_fetch, "taker": cmd_taker, "settle": cmd_settle, "report": cmd_report, "rule": cmd_rule}[a.cmd](a)
