"""Lighter binary-options shadow recorder (BTC/ETH up/down). Read-only: no keys, no orders.

rec:   one websocket. For every series tag, the current and the next window: order_book + trade
       channels. Plus bo_market_stats/all (status, outcome) and the Chainlink 1 s candles Lighter
       settles on. Markets are discovered over REST every 20 s.
check: print the live series and markets, then the first raw messages of each kind (schema check).

Hourly fixed-width files under OUT/YYYY-MM-DD/ (prices in 1e-4 $, asset index as in shadow_rec):
  lb  local_us, exch_ms, asset, tf, epoch, 3 x (bid px, size), 3 x (ask px, size); all-zero = book unknown
  lt  local_us, trade_ms, asset, tf, epoch, px, size, taker_is_buy, ask_account, bid_account, trade_id
  lc  local_us, candle_ts, asset, close
OUT/lighter_markets.csv   asset,tf,epoch,market_id,trade_start_ms,trade_end_ms
OUT/lighter_outcomes.csv  market_id,status,outcome,local_s
"""

import argparse
import asyncio
import json
import re
import struct
import time
import urllib.request
from pathlib import Path

websockets = None  # imported in main so the evaluator can import this module without it

ASSETS = ("btc", "eth", "sol", "xrp", "doge")  # same indices as shadow_rec
TAGS = ("btc-updown-15m", "eth-updown-15m")
REST = "https://mainnet.zklighter.elliot.ai/api/v1/"
WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
LB_REC = struct.Struct("<qqBBI" + "Hf" * 6)
LT_REC = struct.Struct("<qqBBIHfBqqq")
LC_REC = struct.Struct("<qqBd")
NLV = 3


def http_json(path):
    req = urllib.request.Request(REST + path, headers={"User-Agent": "updown-shadow"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def px(s):
    return int(round(float(s) * 10000))


def events(tag=None, limit=40):
    d = http_json(f"events?active=true&sort=start_at&order=asc&limit={limit}" + (f"&tag={tag}" if tag else ""))
    return d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])


def parse_event(e):
    """-> (asset_idx, tf_min, epoch_s, market_id, trade_start_ms, trade_end_ms, status, end_s) or None"""
    try:
        ob = e["order_books"][0]
        m = re.match(r"([A-Za-z]+)-UPDOWN-(\d+)M-(\d+)$", ob["symbol"])
        a = ASSETS.index(m.group(1).lower())
        return (a, int(m.group(2)), int(m.group(3)), int(ob["market_id"]), int(ob.get("start_timestamp") or 0),
                int(ob.get("end_timestamp") or 0), ob.get("status"), int(e["end_at"]) // 1000)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return None


class Out:
    def __init__(self, root):
        self.root, self.buf, self.n = root, {"lb": [], "lt": [], "lc": []}, {"lb": 0, "lt": 0, "lc": 0}

    def add(self, kind, row):
        self.buf[kind].append(row)

    def flush(self):
        t = time.gmtime()
        d = self.root / time.strftime("%Y-%m-%d", t)
        d.mkdir(parents=True, exist_ok=True)
        for k, b in self.buf.items():
            if b:
                with (d / f"{k}-{t.tm_hour:02d}.bin").open("ab") as f:
                    f.write(b"".join(b))
                self.n[k] += len(b)
                b.clear()


class State:
    def __init__(self, out):
        self.out = out
        self.mk = {}  # market_id -> dict(a, tf, e, end, book=({}, {}), last, x)
        self.watch = {}  # market_id -> local_s when it stopped trading; waits for an outcome
        self.seen_trades, self.lastc, self.err = set(), {}, None
        self.raw = {}  # check mode: first raw messages per type

    def top(self, book):
        b = sorted(book[0].items(), reverse=True)[:NLV]
        k = sorted(book[1].items())[:NLV]
        f = lambda v: [x for p in v + [(0, 0.0)] * (NLV - len(v)) for x in p]  # noqa: E731
        return tuple(f(b) + f(k))

    def unknown(self):
        lus = time.time_ns() // 1000
        for m in self.mk.values():
            if m["last"] is not None:
                self.out.add("lb", LB_REC.pack(lus, m["x"] + 1, m["a"], m["tf"], m["e"], *([0, 0.0] * 2 * NLV)))
                m["last"], m["book"] = None, ({}, {})

    def handle(self, raw, lus):
        m = json.loads(raw)
        typ, ch = str(m.get("type", "")), str(m.get("channel", ""))
        if self.raw is not None and len(self.raw.setdefault(typ, [])) < 2:
            self.raw[typ].append(raw if isinstance(raw, str) else raw.decode())
        if "order_book" in m and ch.startswith("order_book"):
            mk = self.mk.get(int(re.split("[:/]", ch)[1]))
            if mk is None:
                return
            ob, book = m["order_book"], mk["book"]
            if typ.startswith("subscribed"):
                book[0].clear()
                book[1].clear()
            for side, key in ((0, "bids"), (1, "asks")):
                for lv in ob.get(key) or ():
                    p, s = px(lv["price"]), float(lv["size"])
                    if s > 0:
                        book[side][p] = s
                    else:
                        book[side].pop(p, None)
            mk["x"] = int(m.get("timestamp") or mk["x"])
            top = self.top(book)
            if top != mk["last"]:
                mk["last"] = top
                self.out.add("lb", LB_REC.pack(lus, mk["x"], mk["a"], mk["tf"], mk["e"], *top))
        elif ch.startswith("trade") and ("trades" in m):
            mk = self.mk.get(int(re.split("[:/]", ch)[1]))
            for t in (m.get("trades") or ()) if mk else ():
                tid = int(t.get("trade_id") or 0)
                if tid in self.seen_trades:
                    continue
                self.seen_trades.add(tid)
                self.out.add("lt", LT_REC.pack(lus, int(t.get("timestamp") or 0), mk["a"], mk["tf"], mk["e"], px(t["price"]),
                                               float(t["size"]), int(bool(t.get("is_maker_ask"))),
                                               int(t.get("ask_account_id") or 0), int(t.get("bid_account_id") or 0), tid))
        elif "bo_market_stats" in m:
            st = m["bo_market_stats"] or {}
            for s in [st] if "market_id" in st else st.values():
                mid = int(s.get("market_id") or 0)
                if mid in self.watch and (s.get("outcome") or s.get("status") == "settled"):
                    with (self.out.root / "lighter_outcomes.csv").open("a") as f:
                        f.write(f"{mid},{s.get('status')},{s.get('outcome')},{int(time.time())}\n")
                    del self.watch[mid]
        elif "candles" in m:
            for c in m["candles"] or ():
                a, ts = str(c.get("symbol", "")).lower(), int(c.get("timestamp") or 0)
                if a in ASSETS and ts >= self.lastc.get(a, 0):
                    self.lastc[a] = ts
                    self.out.add("lc", LC_REC.pack(lus, ts, ASSETS.index(a), float(c["close"])))


async def discover(S, tags):
    known = set()
    while True:
        now = time.time()
        for tag in tags:
            try:
                evs = await asyncio.to_thread(events, tag)
            except Exception as e:  # noqa: BLE001
                S.err = f"events {tag}: {e!r}"[:160]
                continue
            for p in filter(None, map(parse_event, evs)):
                a, tf, ep, mid, ts0, ts1, status, end = p
                if status == "active" and ep <= now + tf * 60 + 60 and end > now and mid not in S.mk:
                    S.mk[mid] = dict(a=a, tf=tf, e=ep, end=end, book=({}, {}), last=None, x=0)
                    if mid not in known:
                        known.add(mid)
                        with (S.out.root / "lighter_markets.csv").open("a") as f:
                            f.write(f"{ASSETS[a]},{tf},{ep},{mid},{ts0},{ts1}\n")
        for mid in [k for k, v in S.mk.items() if v["end"] + 20 < now]:
            del S.mk[mid]
            S.watch[mid] = now
        for mid in [k for k, v in S.watch.items() if v + 3600 < now]:
            del S.watch[mid]
        if len(S.seen_trades) > 200_000:
            S.seen_trades.clear()
        await asyncio.sleep(20)


async def stream(S, tags, stop_at=None):
    static = ["bo_market_stats/all"] + sorted({f"up_down_candle/{t.split('-')[0]}/{t.split('-')[2]}/1s" for t in tags})
    while stop_at is None or time.time() < stop_at:
        try:
            async with websockets.connect(WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                for c in static:
                    await ws.send(json.dumps({"type": "subscribe", "channel": c}))
                subbed, ping = set(), time.time()
                while stop_at is None or time.time() < stop_at:
                    want = {f"{k}/{mid}" for mid in S.mk for k in ("order_book", "trade")}
                    for c in want - subbed:
                        await ws.send(json.dumps({"type": "subscribe", "channel": c}))
                    for c in subbed - want:
                        await ws.send(json.dumps({"type": "unsubscribe", "channel": c}))
                    subbed = want
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 1)
                    except asyncio.TimeoutError:
                        raw = None
                    if time.time() - ping > 30:
                        await ws.send('{"type":"ping"}')
                        ping = time.time()
                    if raw:
                        try:
                            S.handle(raw, time.time_ns() // 1000)
                        except Exception as e:  # noqa: BLE001
                            S.err = f"msg: {e!r} {str(raw)[:120]}"
        except Exception as e:  # noqa: BLE001
            S.err = f"ws: {e!r}"[:160]
        S.unknown()
        await asyncio.sleep(1)


async def housekeeping(S):
    k = 0
    while True:
        await asyncio.sleep(1)
        S.out.flush()
        k += 1
        if k % 60 == 0:
            print(f"rows {S.out.n} markets={len(S.mk)} waiting_outcome={len(S.watch)} last_err={S.err}", flush=True)
            S.err = None


async def run(a):
    a.out.mkdir(parents=True, exist_ok=True)
    S = State(Out(a.out))
    S.raw = None
    await asyncio.gather(discover(S, a.tags), stream(S, a.tags), housekeeping(S))


async def check(a):
    evs = events(limit=100)
    tags = sorted({e.get("tag") for e in evs})
    print("series tags among active events:", tags)
    S = State(Out(Path("/tmp")))
    S.out.add = lambda *_: None
    now = time.time()
    for tag in a.tags:
        ps = [p for p in map(parse_event, events(tag)) if p and p[6] == "active" and p[7] > now]
        print(f"{tag}: {len(ps)} active markets; next:", [(ASSETS[p[0]], p[2], p[3]) for p in ps[:2]])
        for p in ps[:1]:
            S.mk[p[3]] = dict(a=p[0], tf=p[1], e=p[2], end=p[7], book=({}, {}), last=None, x=0)
    await stream(S, a.tags, stop_at=time.time() + 15)
    for typ, msgs in S.raw.items():
        for r in msgs[:1]:
            print(f"\n[{typ}] {r[:900]}")
    for mid, m in S.mk.items():
        print(f"\nmarket {mid} {ASSETS[m['a']]} top3 (bid px,size x3 | ask px,size x3): {m['last']}")
    print("last error:", S.err)


def main():
    global websockets
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("rec")
    p.add_argument("--out", type=Path, required=True)
    for q in (p, sp.add_parser("check")):
        q.add_argument("--tags", nargs="+", default=list(TAGS))
    a = ap.parse_args()
    import websockets

    asyncio.run(run(a) if a.cmd == "rec" else check(a))


if __name__ == "__main__":
    main()
