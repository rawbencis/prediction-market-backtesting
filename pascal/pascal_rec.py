"""Pascal x Polymarket recorder. Read-only: public market data, no keys, no orders.

Pascal  (wss://data.pascal.trade/ws): `book` + `trades` for the top N mapped markets (by Pascal 24 h taker
        notional, then all-time notional, then long interest), plus `all_trades` (trades in markets outside
        the set are written as `pta`) and `market_update`.
Polymarket (wss://ws-subscriptions-clob.polymarket.com/ws/market): both outcome tokens of every recorded
        Pascal market, sharded over several sockets (one Tokyo<->London socket backlogs).
The market list is refreshed every --refresh-h hours from GET /api/v1/markets (the snapshot is kept gzipped).

Output: OUT/YYYY-MM-DD/HH.jsonl.gz (UTC hour of receive time), one JSON object per line, `k` = kind:
  hdr   list of recorded markets: {s: [[symbol, long_token, reverse_token], ...]}
  pb    Pascal top 3 after a book message, written only when the top 3 changed:
        r recv ms (float), s symbol, q seq, st state_time_ms, sv server_time_ms, y s|u, b/a [[px, size] x<=3]
  pspec Pascal MarketSpec as sent with a book snapshot/update
  pt    Pascal trade (trades channel): r, q/st/sv of the message, y, then id, p, sz, ts (trade_ts_ms),
        tq (trade seq), side (taker side BID|ASK), mk/tk (maker/taker wallet), mro/tro, mpp/tpp
  pta   same as pt, from all_trades, for symbols outside the recorded set
  pmu   Pascal market_update payload (raw data field)
  mb    Polymarket top 3 after a book/price_change: r, t (= symbol|L or symbol|R), x exchange ms, b, a
  mt    Polymarket last_trade_price: r, t, x, p, sz, side (taker), fee (bps), h (tx hash)
  mtk   Polymarket tick_size_change (raw)
  gap   a socket dropped; books from that source are unknown until the next snapshot: src, why
  lag   once a minute: feed lag and counters (also printed)
Guards: exits 0 when free disk < --min-free-gb (so Restart=on-failure does not loop), exits 1 when RSS >
--max-rss-mb, stops writing books at 90% of --day-cap-mb (UTC day) and everything at 100%.
"""

import argparse
import asyncio
import gzip
import json
import os
import random
import shutil
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import orjson

    loads = orjson.loads

    def dumps(o):
        return orjson.dumps(o)
except ImportError:  # pragma: no cover
    loads = json.loads

    def dumps(o):
        return json.dumps(o, separators=(",", ":")).encode()


websockets = None  # imported in main so tests can import this module without it

PASCAL_REST = "https://data.pascal.trade/api/v1/markets"
PASCAL_WS = "wss://data.pascal.trade/ws"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {"User-Agent": "pascal-rec research (read-only)"}


def now_ms():
    return time.time_ns() / 1e6


def http_bytes(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def vol24(m, k="last_24h"):
    s = (m.get("stats") or {}).get(k) or {}
    return float(s.get("taker_buy_volume_notional") or 0) + float(s.get("taker_sell_volume_notional") or 0)


def select_markets(markets, n):
    """Top n Pascal markets with a polygon reference -> [(symbol, long_token, reverse_token)]."""
    mp = []
    for m in markets:
        ref = (m.get("display_attributes") or {}).get("reference") or {}
        if ref.get("kind") != "polygon" or m.get("resolution"):
            continue
        if not ref.get("market_outcome_token_id") or not ref.get("reverse_outcome_token_id"):
            continue
        key = (vol24(m), vol24(m, "all_time"), float(m.get("long_interest") or 0))
        mp.append((key, m["symbol"], str(ref["market_outcome_token_id"]), str(ref["reverse_outcome_token_id"])))
    mp.sort(key=lambda x: (-x[0][0], -x[0][1], -x[0][2], x[1]))
    return [(s, lt, rt) for _, s, lt, rt in mp[:n]]


def top3(book, side):
    """book side dict {px: size}; side 'b' (desc) or 'a' (asc) -> [[px, size], ...] best first."""
    ks = sorted(book, reverse=(side == "b"))[:3]
    return [[k, book[k]] for k in ks]


class HourWriter:
    """Hourly gzip JSONL under root/YYYY-MM-DD/HH.jsonl.gz with a per-UTC-day byte cap."""

    def __init__(self, root, day_cap_mb=300.0, flush_s=30.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cap = day_cap_mb * 1e6
        self.flush_s = flush_s
        self.f = None
        self.key = None
        self.last_flush = 0.0
        self.day = None
        self.day_bytes_base = 0  # bytes of the day's closed files (and of this file when it was reopened)
        self.dropped = {"book": 0, "all": 0}
        self.lines = 0
        self.hdr = None  # last hdr object, re-written at the top of each new file

    def _path(self, t):
        d = datetime.fromtimestamp(t, timezone.utc)
        return d.strftime("%Y-%m-%d"), d.strftime("%H")

    def day_bytes(self):
        cur = 0
        if self.f is not None:
            try:
                cur = os.path.getsize(self.fpath)
            except OSError:
                cur = 0
        return self.day_bytes_base + cur

    def _open(self, t):
        day, hh = self._path(t)
        if self.f is not None:
            self.f.close()
        if day != self.day:
            self.day = day
            self.day_bytes_base = 0
            dd = self.root / day
            if dd.exists():  # restarted mid-day: count what is already there
                self.day_bytes_base = sum(p.stat().st_size for p in dd.glob("*.gz"))
        else:
            self.day_bytes_base = self.day_bytes()
        (self.root / day).mkdir(exist_ok=True)
        self.fpath = self.root / day / f"{hh}.jsonl.gz"
        if self.fpath.exists():
            self.day_bytes_base -= self.fpath.stat().st_size
        # append mode: a restart within the hour adds a new gzip member; gzip readers concatenate members
        self.f = gzip.open(self.fpath, "ab", compresslevel=6)
        self.key = (day, hh)
        if self.hdr is not None:
            self.f.write(dumps(self.hdr) + b"\n")

    def add(self, obj, kind="other", t=None):
        t = time.time() if t is None else t
        if self.key != self._path(t):
            self._open(t)
        db = self.day_bytes()
        if db >= self.cap:
            self.dropped["all"] += 1
            return False
        if kind == "book" and db >= 0.9 * self.cap:
            self.dropped["book"] += 1
            return False
        if obj.get("k") == "hdr":
            self.hdr = obj
        self.f.write(dumps(obj) + b"\n")
        self.lines += 1
        if t - self.last_flush > self.flush_s:
            self.f.flush()
            self.last_flush = t
        return True

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None


class Stats:
    def __init__(self):
        self.reset()
        self.pas_conn = 0
        self.pm_conn = 0
        self.pas_drops = 0
        self.pm_drops = 0
        self.err = ""
        self.rtt = []

    def reset(self):
        self.pas_lag_st = []  # recv - state_time_ms (ms)
        self.pas_lag_sv = []  # recv - server_time_ms
        self.pm_lag = []  # recv - exchange timestamp
        self.n = {"pb": 0, "pt": 0, "pta": 0, "mb": 0, "mt": 0, "pmsg": 0, "mmsg": 0}


def qstats(v):
    if not v:
        return None
    v = sorted(v)
    return {"n": len(v), "p10": round(v[len(v) // 10], 1), "p50": round(statistics.median(v), 1),
            "p90": round(v[min(len(v) - 1, (9 * len(v)) // 10)], 1)}


def parse_levels(levels):
    out = {}
    for lv in levels or ():
        out[float(lv[0])] = int(float(lv[1]))
    return out


def trade_obj(k, tr, r, msg):
    return {"k": k, "r": r, "q": msg.get("seq"), "st": msg.get("state_time_ms"), "sv": msg.get("server_time_ms"),
            "y": (msg.get("type") or "u")[0], "id": tr.get("trade_id"), "s": tr.get("symbol"),
            "p": float(tr["price"]), "sz": int(float(tr["size"])), "ts": int(tr.get("trade_ts_ms") or 0),
            "tq": tr.get("seq"), "side": tr.get("taker_side"), "mk": tr.get("maker"), "tk": tr.get("taker"),
            "mro": tr.get("maker_reduce_only"), "tro": tr.get("taker_reduce_only"),
            "mpp": tr.get("maker_position_size_prev"), "tpp": tr.get("taker_position_size_prev")}


class PascalState:
    """Applies Pascal market-data messages; returns the lines to write."""

    def __init__(self, symbols):
        self.symbols = set(symbols)
        self.books = {}
        self.last = {}

    def on_msg(self, msg, r):
        out = []
        ch = msg.get("channel")
        typ = msg.get("type")
        if typ == "error":
            out.append(({"k": "perr", "r": r, "msg": msg}, "other"))
            return out
        if ch == "book":
            s = msg.get("symbol")
            d = msg.get("data") or {}
            if typ == "snapshot" or s not in self.books:
                self.books[s] = ({}, {})
                if typ != "snapshot":
                    out.append(({"k": "perr", "r": r, "why": "update before snapshot", "s": s}, "other"))
            b, a = self.books[s]
            if typ == "snapshot":
                b.update(parse_levels(d.get("bids")))
                a.update(parse_levels(d.get("asks")))
            else:
                for side, src in ((b, d.get("bids")), (a, d.get("asks"))):
                    for p, sz in parse_levels(src).items():
                        if sz > 0:
                            side[p] = sz
                        else:
                            side.pop(p, None)
            if d.get("spec"):
                out.append(({"k": "pspec", "r": r, "s": s, "q": msg.get("seq"), "spec": d["spec"]}, "other"))
            tb, ta = top3(b, "b"), top3(a, "a")
            if typ == "snapshot" or self.last.get(s) != (tb, ta):
                self.last[s] = (tb, ta)
                out.append(({"k": "pb", "r": r, "s": s, "q": msg.get("seq"), "st": msg.get("state_time_ms"),
                             "sv": msg.get("server_time_ms"), "y": typ[0], "b": tb, "a": ta}, "book"))
        elif ch == "trades":
            for tr in msg.get("data") or ():
                out.append((trade_obj("pt", tr, r, msg), "trade"))
        elif ch == "all_trades":
            for tr in msg.get("data") or ():
                if tr.get("symbol") not in self.symbols:
                    out.append((trade_obj("pta", tr, r, msg), "trade"))
        elif ch == "market_update":
            out.append(({"k": "pmu", "r": r, "y": (typ or "u")[0], "q": msg.get("seq"), "data": msg.get("data")},
                        "book" if typ == "update" else "other"))
        return out


class PolyState:
    """Applies Polymarket market-channel messages for the tokens in `names` ({token: 'SYM|L'})."""

    def __init__(self, names):
        self.names = names
        self.books = {t: ({}, {}) for t in names}
        self.last = {}

    def on_raw(self, raw, r):
        if raw in ("PONG", b"PONG", "", b""):
            return []
        ev = loads(raw)
        out = []
        for e in ev if isinstance(ev, list) else (ev,):
            if not isinstance(e, dict):
                continue
            et = e.get("event_type")
            x = int(e.get("timestamp") or 0)
            if et == "last_trade_price":
                t = e.get("asset_id")
                if t in self.names:
                    out.append(({"k": "mt", "r": r, "t": self.names[t], "x": x, "p": float(e["price"]),
                                 "sz": float(e["size"]), "side": e.get("side"), "fee": e.get("fee_rate_bps"),
                                 "h": e.get("transaction_hash")}, "trade"))
                continue
            if et == "tick_size_change":
                out.append(({"k": "mtk", "r": r, "e": e}, "other"))
                continue
            touched = set()
            if et == "book":
                t = e.get("asset_id")
                if t not in self.books:
                    continue
                self.books[t] = tuple(
                    {float(v["price"]): float(v["size"]) for v in e.get(s) or () if float(v["size"]) > 0}
                    for s in ("bids", "asks"))
                touched.add(t)
            elif et == "price_change":
                ch = e.get("price_changes")
                if ch is None:
                    ch = [dict(c, asset_id=e.get("asset_id")) for c in e.get("changes") or ()]
                for c in ch:
                    t = c.get("asset_id")
                    if t not in self.books:
                        continue
                    side = self.books[t][0 if c.get("side") == "BUY" else 1]
                    p, s = float(c["price"]), float(c["size"])
                    if s > 0:
                        side[p] = s
                    else:
                        side.pop(p, None)
                    touched.add(t)
            else:
                continue
            for t in touched:
                b, a = self.books[t]
                tb, ta = top3(b, "b"), top3(a, "a")
                if et == "book" or self.last.get(t) != (tb, ta):
                    self.last[t] = (tb, ta)
                    out.append(({"k": "mb", "r": r, "t": self.names[t], "x": x, "b": tb, "a": ta, "y": et[0]},
                                "book"))
        return out


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


class Recorder:
    def __init__(self, args):
        self.a = args
        self.w = HourWriter(args.out, args.day_cap_mb)
        self.st = Stats()
        self.markets = []
        self.gen = 0  # bumped on every list refresh; socket tasks exit when it changes
        self.stop = False
        self.exit_code = 0

    def load_list(self):
        if self.a.markets_file:
            raw = Path(self.a.markets_file).read_bytes()
        else:
            raw = http_bytes(PASCAL_REST)
            snap = Path(self.a.out) / "markets"
            snap.mkdir(exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with gzip.open(snap / f"markets_{ts}.json.gz", "wb") as f:
                f.write(raw)
        d = loads(raw)
        ms = d["data"] if isinstance(d, dict) else d
        sel = select_markets(ms, self.a.n)
        if not sel:
            raise RuntimeError("no mapped markets")
        self.markets = sel
        self.gen += 1
        self.w.add({"k": "hdr", "r": now_ms(), "gen": self.gen, "s": [list(x) for x in sel]})
        print(f"[list] gen {self.gen}: {len(sel)} markets; first {sel[0][0]}", flush=True)

    async def refresher(self):
        while not self.stop:
            await asyncio.sleep(self.a.refresh_h * 3600)
            try:
                old = self.markets
                await asyncio.to_thread(self.load_list)
                if [m[0] for m in old] == [m[0] for m in self.markets]:
                    continue  # same set: the bumped gen just reconnects, harmless; keep it simple
            except Exception as e:  # noqa: BLE001
                self.st.err = f"refresh: {e!r}"[:200]

    def gap(self, src, why):
        self.w.add({"k": "gap", "r": now_ms(), "src": src, "why": why[:200]})

    async def pascal_task(self):
        backoff = 1.0
        while not self.stop:
            gen = self.gen
            syms = [m[0] for m in self.markets]
            ps = PascalState(syms)
            up = False
            try:
                async with websockets.connect(PASCAL_WS, open_timeout=10, ping_interval=None, max_size=None,
                                              additional_headers=UA) as ws:
                    chans = [{"channel": "all_trades"}, {"channel": "market_update"}]
                    for s in syms:
                        chans += [{"channel": "book", "symbol": s}, {"channel": "trades", "symbol": s}]
                    for i in range(0, len(chans), 100):
                        await ws.send(json.dumps({"type": "subscribe", "channels": chans[i:i + 100]}))
                    up = True
                    self.st.pas_conn += 1
                    backoff = 1.0
                    nxt_ping, ping_t = time.time() + 5, None
                    while not self.stop and gen == self.gen:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 2)
                        except asyncio.TimeoutError:
                            raw = None
                        r = now_ms()
                        if time.time() >= nxt_ping:
                            ping_t = time.perf_counter()
                            await ws.send('{"type":"ping"}')
                            nxt_ping = time.time() + 15
                        if raw is None:
                            continue
                        msg = loads(raw)
                        if isinstance(msg, dict) and msg.get("type") == "pong":
                            if ping_t is not None:
                                self.st.rtt.append((time.perf_counter() - ping_t) * 1000)
                                ping_t = None
                            continue
                        self.st.n["pmsg"] += 1
                        stt, svt = msg.get("state_time_ms"), msg.get("server_time_ms")
                        if msg.get("type") == "update":
                            if stt:
                                self.st.pas_lag_st.append(r - float(stt))
                            if svt:
                                self.st.pas_lag_sv.append(r - float(svt))
                        for obj, kind in ps.on_msg(msg, r):
                            if self.w.add(obj, kind, r / 1000):
                                if obj["k"] in self.st.n:
                                    self.st.n[obj["k"]] += 1
            except Exception as e:  # noqa: BLE001
                self.st.err = f"pascal: {e!r}"[:200]
            if up:
                self.st.pas_conn -= 1
                if gen == self.gen:
                    self.st.pas_drops += 1
                self.gap("pas", "refresh" if gen != self.gen else self.st.err)
            if not self.stop and gen == self.gen:
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)

    async def pm_shard(self, names):
        backoff = 1.0
        gen = self.gen
        while not self.stop and gen == self.gen:
            ps = PolyState(names)
            up = False
            try:
                async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=None,
                                              additional_headers=UA) as ws:
                    await ws.send(json.dumps({"assets_ids": list(names), "type": "market"}))
                    up = True
                    self.st.pm_conn += 1
                    backoff = 1.0
                    nxt = time.time() + 10
                    while not self.stop and gen == self.gen:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 2)
                        except asyncio.TimeoutError:
                            raw = None
                        r = now_ms()
                        if time.time() >= nxt:
                            await ws.send("PING")
                            nxt = time.time() + 10
                        if raw is None:
                            continue
                        self.st.n["mmsg"] += 1
                        for obj, kind in ps.on_raw(raw, r):
                            if obj["k"] == "mb" and obj["x"] and obj["y"] == "p":
                                self.st.pm_lag.append(r - obj["x"])
                            if self.w.add(obj, kind, r / 1000):
                                if obj["k"] in self.st.n:
                                    self.st.n[obj["k"]] += 1
            except Exception as e:  # noqa: BLE001
                self.st.err = f"pm: {e!r}"[:200]
            if up:
                self.st.pm_conn -= 1
                if gen == self.gen:
                    self.st.pm_drops += 1
                self.gap("pm", f"shard {next(iter(names.values()), '')}: {self.st.err}")
            if not self.stop and gen == self.gen:
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)

    async def pm_task(self):
        while not self.stop:
            gen = self.gen
            names = {}
            for s, lt, rt in self.markets:
                names[lt] = f"{s}|L"
                names[rt] = f"{s}|R"
            toks = list(names)
            k = self.a.pm_shard
            shards = [{t: names[t] for t in toks[i:i + k]} for i in range(0, len(toks), k)]
            tasks = [asyncio.create_task(self.pm_shard(sh)) for sh in shards]
            while not self.stop and gen == self.gen:
                await asyncio.sleep(1)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def monitor(self):
        while not self.stop:
            await asyncio.sleep(self.a.log_s)
            fg, rss = free_gb(self.a.out), rss_mb()
            s = self.st
            lag = {"k": "lag", "r": now_ms(), "pas_st": qstats(s.pas_lag_st), "pas_sv": qstats(s.pas_lag_sv),
                   "pm": qstats(s.pm_lag), "rtt": qstats(s.rtt), "n": s.n, "pas_conn": s.pas_conn,
                   "pm_conn": s.pm_conn, "pas_drops": s.pas_drops, "pm_drops": s.pm_drops,
                   "day_mb": round(self.w.day_bytes() / 1e6, 2), "dropped": self.w.dropped,
                   "free_gb": round(fg, 1), "rss_mb": round(rss, 1), "err": s.err, "gen": self.gen}
            self.w.add(lag)
            print(json.dumps(lag, separators=(",", ":")), flush=True)
            s.reset()
            s.rtt = []
            if fg < self.a.min_free_gb:
                print(f"[guard] free disk {fg:.1f} GB < {self.a.min_free_gb}: exiting 0", flush=True)
                self.stop, self.exit_code = True, 0
            elif rss > self.a.max_rss_mb:
                print(f"[guard] RSS {rss:.0f} MB > {self.a.max_rss_mb}: exiting 1", flush=True)
                self.stop, self.exit_code = True, 1
            if self.a.duration_s and time.time() - self.t0 > self.a.duration_s:
                self.stop = True

    async def run(self):
        self.t0 = time.time()
        if free_gb(self.a.out) < self.a.min_free_gb:
            print("[guard] free disk below floor at start: exiting 0", flush=True)
            return 0
        await asyncio.to_thread(self.load_list)
        tasks = [asyncio.create_task(c) for c in (self.pascal_task(), self.pm_task(), self.monitor())]
        if not self.a.markets_file:
            tasks.append(asyncio.create_task(self.refresher()))
        while not self.stop:
            await asyncio.sleep(0.5)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.w.close()
        return self.exit_code


def main(argv=None):
    global websockets
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=os.path.expanduser("~/pascal-data/rec"))
    p.add_argument("--n", type=int, default=150, help="markets to record")
    p.add_argument("--refresh-h", type=float, default=6.0)
    p.add_argument("--pm-shard", type=int, default=20, help="Polymarket tokens per socket")
    p.add_argument("--day-cap-mb", type=float, default=300.0)
    p.add_argument("--min-free-gb", type=float, default=20.0)
    p.add_argument("--max-rss-mb", type=float, default=300.0)
    p.add_argument("--log-s", type=float, default=60.0)
    p.add_argument("--duration-s", type=float, default=0.0, help="stop after this many seconds (0 = run)")
    p.add_argument("--markets-file", help="use this /api/v1/markets JSON instead of fetching (no refresh)")
    a = p.parse_args(argv)
    import websockets as _ws

    websockets = _ws
    Path(a.out).mkdir(parents=True, exist_ok=True)
    return asyncio.run(Recorder(a).run())


if __name__ == "__main__":
    sys.exit(main())
