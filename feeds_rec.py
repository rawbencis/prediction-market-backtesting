"""Extra feed recorder (Dublin): Chainlink spot + 60 s TWAP (legacy RTDS), Coinbase, Deribit, Polymarket BTC 5m/15m depth.
Hourly gzip JSONL under OUT/YYYY-MM-DD/<feed>-HH.jsonl.gz. Row: {"r": local ns, "x": venue ms, ...}. Read-only, no keys.
Usage: python feeds_rec.py --out ~/feeds-rec
"""
import argparse, asyncio, gzip, json, time, urllib.request
from datetime import datetime
from pathlib import Path

import orjson
import websockets

RTDS = "wss://ws-live-data.polymarket.com"
CB = "wss://ws-feed.exchange.coinbase.com"
DB = "wss://www.deribit.com/ws/api/v2"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com/markets?slug="
SYMS = {"btc/usd", "eth/usd", "sol/usd", "xrp/usd", "doge/usd"}
CB_PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"]
DEPTH, LEAD_S = 5, 45
STATS = {}


class Out:
    def __init__(self, root):
        self.root, self.files, self.n = Path(root), {}, {}

    def add(self, feed, row):
        h = time.strftime("%Y-%m-%d/%H", time.gmtime())
        f = self.files.get(feed)
        if f is None or f[0] != h:
            if f:
                f[1].close()
            d, hh = h.split("/")
            (self.root / d).mkdir(parents=True, exist_ok=True)
            f = self.files[feed] = (h, gzip.open(self.root / d / f"{feed}-{hh}.jsonl.gz", "ab", compresslevel=5))
        f[1].write(orjson.dumps(row) + b"\n")
        self.n[feed] = self.n.get(feed, 0) + 1

    def flush(self):
        for _, g in self.files.values():
            g.flush()


def log(*a):
    print(datetime.utcnow().isoformat(timespec="seconds"), *a, flush=True)


async def keep(name, fn, out):
    """Run fn(out) forever; reconnect with exponential backoff (1 s .. 60 s), log every reconnect."""
    wait = 1
    while True:
        t0 = time.time()
        try:
            await fn(out)
        except Exception as e:  # noqa: BLE001
            log("reconnect", name, repr(e)[:200])
        STATS[name + "_reconnects"] = STATS.get(name + "_reconnects", 0) + 1
        wait = 1 if time.time() - t0 > 60 else min(wait * 2, 60)
        await asyncio.sleep(wait)


async def rtds(out):
    async with websockets.connect(RTDS, open_timeout=10, ping_interval=None, max_size=None) as ws:
        subs = [{"topic": t, "type": "*", "filters": ""} for t in ("crypto_prices_chainlink", "crypto_prices_twap_sixty")]
        await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
        nxt = time.time() + 5
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                raw = None
            r = time.time_ns()
            if r / 1e9 > nxt:
                await ws.send("PING")
                nxt = r / 1e9 + 5
            if not raw or raw in ("PONG", b"PONG"):
                continue
            try:
                m = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            p, t = m.get("payload") or {}, m.get("topic")
            feed = "chainlink" if t == "crypto_prices_chainlink" else "twap60" if t == "crypto_prices_twap_sixty" else None
            if feed and m.get("type") == "update" and p.get("symbol") in SYMS:
                out.add(feed, {"r": r, "x": p.get("timestamp"), "s": p["symbol"].split("/")[0], "v": p.get("value"),
                               "fa": p.get("full_accuracy_value"), "ts": m.get("timestamp")})
            elif feed and m.get("type") == "subscribe":  # 2-min snapshot after (re)connect: keep, flagged
                for d in p.get("data") or ():
                    if p.get("symbol") in SYMS:
                        out.add(feed, {"r": r, "x": d.get("timestamp"), "s": p["symbol"].split("/")[0], "v": d.get("value"),
                                       "fa": d.get("full_accuracy_value"), "snap": 1})


def iso_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000) if s else None


async def coinbase(out):
    async with websockets.connect(CB, open_timeout=10, max_size=None) as ws:
        await ws.send(json.dumps({"type": "subscribe", "product_ids": CB_PRODUCTS, "channels": ["matches", "ticker", "heartbeat"]}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), 30)
            r = time.time_ns()
            m = orjson.loads(raw)
            ty = m.get("type")
            if ty in ("match", "last_match", "ticker"):
                m["r"], m["x"] = r, iso_ms(m.get("time"))
                out.add("coinbase", m)
            elif ty == "error":
                raise RuntimeError(m)


async def deribit(out):
    async with websockets.connect(DB, open_timeout=10, max_size=None) as ws:
        chans = [f"{k}.{i}-PERPETUAL.{iv}" for i in ("BTC", "ETH") for k, iv in (("trades", "raw"), ("ticker", "100ms"))]
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe", "params": {"channels": chans}}))
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "public/set_heartbeat", "params": {"interval": 30}}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), 60)
            r = time.time_ns()
            m = orjson.loads(raw)
            if m.get("id") == 1:
                got = m.get("result")
                log("deribit subscribed", got, m.get("error"))
                if not got or len(got) < len(chans):  # raw trades need auth on some accounts: fall back to 100ms
                    alt = [c.replace(".raw", ".100ms") for c in chans if c not in (got or [])]
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "public/subscribe", "params": {"channels": alt}}))
            elif m.get("id") == 3:
                log("deribit fallback subscribed", m.get("result"), m.get("error"))
            elif m.get("method") == "heartbeat":
                if (m.get("params") or {}).get("type") == "test_request":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "public/test", "params": {}}))
            elif m.get("method") == "subscription":
                pr = m["params"]
                data, ch = pr["data"], pr["channel"]
                if ch.startswith("trades"):
                    for t in data:
                        out.add("deribit", {"r": r, "x": t.get("timestamp"), "ch": ch, "p": t.get("price"),
                                            "a": t.get("amount"), "side": t.get("direction"), "id": t.get("trade_id")})
                else:
                    out.add("deribit", {"r": r, "x": data.get("timestamp"), "ch": ch, "bid": data.get("best_bid_price"),
                                        "bq": data.get("best_bid_amount"), "ask": data.get("best_ask_price"),
                                        "aq": data.get("best_ask_amount"), "mark": data.get("mark_price"),
                                        "idx": data.get("index_price"), "last": data.get("last_price")})


# ---- Polymarket depth ----
def apply_msg(books, ev):
    """Apply one market-channel event to books {token: (bids{px: size}, asks{px: size})}.
    -> set of tokens touched. Prices kept as strings' float values."""
    et = ev.get("event_type")
    if et == "book":
        a = ev.get("asset_id")
        if a not in books:
            return set()
        books[a] = tuple({float(v["price"]): float(v["size"]) for v in ev.get(s) or () if float(v["size"]) > 0}
                         for s in ("bids", "asks"))
        return {a}
    if et == "price_change":
        ch = ev.get("price_changes")
        if ch is None:
            ch = [dict(c, asset_id=ev.get("asset_id")) for c in ev.get("changes") or ()]
        touched = set()
        for c in ch:
            a = c.get("asset_id")
            if a not in books:
                continue
            side = books[a][0 if c["side"] == "BUY" else 1]
            p, s = float(c["price"]), float(c["size"])
            if s > 0:
                side[p] = s
            else:
                side.pop(p, None)
            touched.add(a)
        return touched
    return set()


def top(book, n=DEPTH):
    b, a = book
    return [[p, b[p]] for p in sorted(b, reverse=True)[:n]], [[p, a[p]] for p in sorted(a)[:n]]


async def pm_window(tf, epoch, out):
    sl = f"btc-updown-{tf}m-{epoch}"
    toks = None
    for _ in range(20):
        try:
            m = (await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(GAMMA + sl, timeout=10))))
            if m:
                ids, names = json.loads(m[0]["clobTokenIds"]), [n.lower() for n in json.loads(m[0]["outcomes"])]
                toks = dict(zip(ids, names))
                break
        except Exception as e:  # noqa: BLE001
            log("gamma", sl, repr(e)[:120])
        await asyncio.sleep(3)
    if not toks:
        log("no market", sl)
        return
    end = epoch + tf * 60 + 3
    while time.time() < end:
        books, last = {t: ({}, {}) for t in toks}, {}
        try:
            async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": list(toks), "type": "market"}))
                nxt = time.time() + 10
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        raw = None
                    r = time.time_ns()
                    if r / 1e9 > nxt:
                        await ws.send("PING")
                        nxt = r / 1e9 + 10
                    if not raw or raw in ("PONG", b"PONG"):
                        continue
                    ev = orjson.loads(raw)
                    for e in ev if isinstance(ev, list) else (ev,):
                        x = int(e.get("timestamp") or 0)
                        if e.get("event_type") == "last_trade_price" and e.get("asset_id") in toks:
                            out.add("pmbook", {"r": r, "x": x, "slug": sl, "tok": toks[e["asset_id"]], "ev": "trade",
                                               "p": e.get("price"), "sz": e.get("size"), "side": e.get("side"),
                                               "fee": e.get("fee_rate_bps"), "tx": e.get("transaction_hash")})
                            continue
                        for a in apply_msg(books, e):
                            bb, aa = top(books[a])
                            if (bb, aa) != last.get(a):
                                last[a] = (bb, aa)
                                out.add("pmbook", {"r": r, "x": x, "slug": sl, "tok": toks[a], "b": bb, "a": aa})
        except Exception as e:  # noqa: BLE001
            log("reconnect pm", sl, repr(e)[:160])
            STATS["pm_reconnects"] = STATS.get("pm_reconnects", 0) + 1
            if time.time() < end:
                out.add("pmbook", {"r": time.time_ns(), "x": 0, "slug": sl, "ev": "drop"})
                await asyncio.sleep(1)


async def pm_sched(out):
    seen, tasks = set(), set()
    while True:
        now = time.time()
        for tf in (5, 15):
            w = tf * 60
            for e in (int(now // w) * w, int(now // w) * w + w):
                if (tf, e) not in seen and now >= e - LEAD_S and now < e + w:
                    seen.add((tf, e))
                    t = asyncio.create_task(pm_window(tf, e, out))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        seen = {k for k in seen if k[1] > now - 7200}
        await asyncio.sleep(1)


async def housekeeping(out):
    k = 0
    while True:
        await asyncio.sleep(5)
        out.flush()
        k += 1
        if k % 60 == 0:
            log("rows", dict(out.n), "stats", dict(STATS))


async def run(a):
    out = Out(a.out)
    await asyncio.gather(keep("rtds", rtds, out), keep("coinbase", coinbase, out), keep("deribit", deribit, out),
                         pm_sched(out), housekeeping(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "feeds-rec"))
    asyncio.run(run(ap.parse_args()))
