"""T1 real-time PAPER trader: Lighter x Polymarket BTC 15m arb above 4c. Paper only.

    python3 lp_arb/paper.py [--out ~/lp-arb/paper] [--minutes N]

What it does: one asyncio process watches the live BTC 15m up/down window on both venues (Lighter read-only
websocket: order book + trades; Polymarket market websocket: the window's Up token book) and on every book
update evaluates the frozen T1 trigger (frozen/t1_lighter_pm_arb.json). On a trigger it logs what the bot would
do, then checks the Lighter quote at +100/+300/+500 ms (+500 ms displayed == the paper Lighter fill, as in the
frozen rule), logs the Polymarket price the hedge would get at +1000 ms (500 ms after the Lighter match), and at
settlement logs the paper P&L of every hedged pair. It rolls to the next window by itself.

Frozen rule, as applied here (same constants as lighter_eval @1348b2e):
  trigger   pair (Up on one venue + Down on the other) costs > 4c under $1 after both taker fees,
            min(Lighter size at best, PM hedge size at best) >= 5 shares, both books valid
            side a: buy Lighter Up at Lighter ask + buy PM Down at 1 - PM Up bid
            side b: buy Lighter Down (sell Up) at Lighter bid + buy PM Up at PM Up ask
  window    only from window start + 5 s, and only if the hedge time (trigger + 1 s) is before the last
            250 ms grid point of the window (window end - 0.25 s)
  Lighter   fills 500 ms after the trigger if its quote is still >= 5 shares at the trigger price or better
  hedge     500 ms after that, at the prevailing PM price; clip = min(Lighter size at +500, PM size at +1000, 200)
  pause     per side: no new trigger for 500 ms after a trigger, and for 5 s after a Lighter fill
  fees      Lighter 0.01 p(1-p); Polymarket rate * p(1-p), rate from Gamma's feeSchedule for the live market
Differences from the batch shadow (by design, reported by paper_vs_shadow.py): this process evaluates on every
book update, not on the 250 ms grid, and its books come from its own sockets.

PAPER ONLY. No signing library is imported and no order endpoint is called. The two live-order functions
below raise NotImplementedError; the probe caps beside them are the ones in frozen/t1_shadow_candidate.json.

Log: OUT/YYYY-MM-DD.jsonl (UTC date of the write; a new file each UTC day), at most 200 MB per day (after that
only a single cap line is written until the day rolls). Kinds: start, window, trigger, check, hedge, settle,
health, error, cap.
"""

import argparse
import asyncio
import json
import os
import random
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

import websockets

# ---- frozen rule constants (lighter_eval @1348b2e / frozen/t1_lighter_pm_arb.json) ----
TH = 0.04
SIZE, CAP = 5.0, 200.0
L_MATCH_MS, HEDGE_MS = 500, 500
COOL_S = 5.0
START_OFFSET_S = 5.0
GRID_S = 0.25
LIGHTER_FEE = 0.01

# ---- probe caps (frozen/t1_shadow_candidate.json, min_size_probe_after_shadow_pass); NOT USED in paper mode ----
PROBE_SHARES_PER_LEG = 5
PROBE_MAX_PAIRS_PER_DAY = 20
PROBE_MAX_OPEN_COST_USD = 25.0
PROBE_PM_LIMIT_OVER_ASK = 0.01  # PM FAK hedge limit = prevailing ask + 1c, no chase beyond that
PROBE_ASSETS = ("btc",)
PROBE_STOP_UNHEDGED_SHARE = 0.10
PROBE_STOP_AFTER_PAIRS_NEGATIVE = 50


def live_lighter_ioc(market_id, is_buy_up, price, shares):
    """Live Lighter IOC at the trigger price. Needs operator approval, a separate deploy and a signer. Not in paper mode."""
    raise NotImplementedError("paper mode: no Lighter orders (probe needs operator approval and a separate deploy)")


def live_pm_fak_hedge(token_id, limit_price, shares):
    """Live Polymarket FAK hedge, limit = ask + PROBE_PM_LIMIT_OVER_ASK, no chase. Not in paper mode."""
    raise NotImplementedError("paper mode: no Polymarket orders (probe needs operator approval and a separate deploy)")


L_REST = "https://mainnet.zklighter.elliot.ai/api/v1/"
L_WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com/markets?"
TAG = "btc-updown-15m"
OUT = Path.home() / "lp-arb" / "paper"
CAP_BYTES = 200 * 1024 * 1024
KEEP_DAYS = 60  # paper logs older than this are deleted at the UTC day roll


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lp-arb-paper"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def px(s):
    return round(float(s), 4)


def fl(p):
    return LIGHTER_FEE * p * (1 - p)


class Log:
    def __init__(self, root):
        self.root, self.day, self.f, self.bytes, self.capped = root, None, None, 0, False
        root.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind, **kw):
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.day:
            if self.f:
                self.f.close()
            self.day = day
            p = self.root / f"{day}.jsonl"
            self.f = p.open("a", buffering=1)
            self.bytes, self.capped = p.stat().st_size, False
            cut = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS * 86400))
            for old in self.root.glob("????-??-??.jsonl"):
                if old.stem < cut:
                    old.unlink(missing_ok=True)
        if self.capped:
            return
        line = json.dumps(dict(kind=kind, t_local=round(now, 6), **kw), separators=(",", ":"), default=str) + "\n"
        if self.bytes + len(line) > CAP_BYTES:
            self.f.write(json.dumps(dict(kind="cap", t_local=now, msg="200 MB/day reached; logging paused until the UTC day rolls")) + "\n")
            self.capped = True
            return
        self.f.write(line)
        self.bytes += len(line)


class Book:
    """Price-level book; best() -> (bid, ask, bid_size, ask_size)."""

    def __init__(self):
        self.b, self.a, self.x, self.t = {}, {}, 0, 0.0

    def clear(self):
        self.b.clear()
        self.a.clear()

    def set(self, side, p, s):
        d = self.b if side == 0 else self.a
        if s > 0:
            d[p] = s
        else:
            d.pop(p, None)

    def best(self):
        bb = max(self.b) if self.b else 0.0
        ba = min(self.a) if self.a else 0.0
        return bb, ba, self.b.get(bb, 0.0), self.a.get(ba, 0.0)

    def valid(self):
        bb, ba, _, _ = self.best()
        return bb > 0 and ba > bb


class Window:
    def __init__(self, e, mid):
        self.e, self.end, self.mid = e, e + 900, mid
        self.lbook, self.pbook = Book(), Book()
        self.up_token = self.down_token = self.slug = None
        self.pm_rate = None
        self.ftype = ""
        self.nxt = {"a": 0.0, "b": 0.0}
        self.pairs = []  # hedged paper pairs
        self.n_trig = 0
        self.ltrades = []  # (local s, exch ms, px, size, taker bought Up) in the last 5 s


class Paper:
    def __init__(self, log, stop_at=None):
        self.log, self.stop_at = log, stop_at
        self.win = {}  # epoch -> Window
        self.by_mid = {}  # lighter market id -> Window
        self.known_mids, self.l_outcomes = set(), {}  # Lighter outcomes from bo_market_stats/all
        self.stats = dict(l_msgs=0, p_msgs=0, l_reconn=0, p_reconn=0, l_lag=[], p_lag=[], errors=0, triggers=0)
        self.stop = asyncio.Event()

    # ---------- discovery ----------
    async def discover(self):
        while not self.stop.is_set():
            try:
                d = await asyncio.to_thread(http_json, L_REST + f"events?active=true&sort=start_at&order=asc&limit=40&tag={TAG}")
                evs = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
                now = time.time()
                for ev in evs:
                    try:
                        ob = ev["order_books"][0]
                        m = re.match(r"BTC-UPDOWN-15M-(\d+)$", ob["symbol"], re.I)
                        e, mid = int(m.group(1)), int(ob["market_id"])
                    except (KeyError, IndexError, TypeError, AttributeError):
                        continue
                    if e - 60 <= now < e + 900 and e not in self.win:
                        w = Window(e, mid)
                        self.win[e], self.by_mid[mid] = w, w
                        self.known_mids.add(mid)
                        asyncio.create_task(self.pm_meta_and_socket(w))
                for e in [k for k, w in self.win.items() if w.end + 5 < now]:
                    w = self.win.pop(e)
                    self.by_mid.pop(w.mid, None)
                    asyncio.create_task(self.settle(w))
            except Exception as ex:  # noqa: BLE001
                self.err("discover", ex)
            await self.sleep(20)

    async def pm_meta_and_socket(self, w):
        slug = f"btc-updown-15m-{w.e}"
        for _ in range(30):
            try:
                ms = await asyncio.to_thread(http_json, GAMMA + f"slug={slug}")
                m = ms[0]
                names = [n.upper() for n in json.loads(m["outcomes"])]
                toks = json.loads(m["clobTokenIds"])
                w.up_token, w.down_token, w.slug = toks[names.index("UP")], toks[names.index("DOWN")], slug
                fs = m.get("feeSchedule") or {}
                w.pm_rate, w.ftype = float(fs.get("rate", 0.05)), m.get("feeType") or ""
                break
            except Exception as ex:  # noqa: BLE001
                self.err("gamma", ex)
                await self.sleep(5)
        if not w.up_token:
            return
        self.log("window", e=w.e, slug=slug, lighter_market=w.mid, pm_fee_rate=w.pm_rate, pm_fee_type=w.ftype,
                 up_token=w.up_token, down_token=w.down_token)
        await self.pm_socket(w)

    # ---------- Polymarket ----------
    async def pm_socket(self, w):
        backoff = 1.0
        while time.time() < w.end + 3 and not self.stop.is_set():
            try:
                async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"assets_ids": [w.up_token], "type": "market"}))
                    backoff, nxt_ping = 1.0, time.time() + 10
                    while time.time() < w.end + 3 and not self.stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 5)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.time() >= nxt_ping:
                            await ws.send("PING")
                            nxt_ping = time.time() + 10
                        if raw and raw not in ("PONG", b"PONG"):
                            try:
                                self.on_pm(w, raw)
                            except Exception as ex:  # noqa: BLE001
                                self.err("pm_msg", ex, str(raw)[:200])
            except Exception as ex:  # noqa: BLE001
                self.err("pm_ws", ex)
            if time.time() < w.end + 3 and not self.stop.is_set():
                self.stats["p_reconn"] += 1
                w.pbook.clear()  # book unknown until the next snapshot
                await self.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30)

    def on_pm(self, w, raw):
        lus = time.time()
        ev = json.loads(raw)
        changed = False
        for e in ev if isinstance(ev, list) else (ev,):
            et = e.get("event_type")
            if et == "book" and e.get("asset_id") == w.up_token:
                w.pbook.clear()
                for side, key in ((0, "bids"), (1, "asks")):
                    for v in e.get(key) or ():
                        w.pbook.set(side, px(v["price"]), float(v["size"]))
                changed = True
            elif et == "price_change":
                for c in e.get("price_changes") or ():
                    if c.get("asset_id") != w.up_token:
                        continue
                    w.pbook.set(0 if c["side"] == "BUY" else 1, px(c["price"]), float(c["size"]))
                    changed = True
            else:
                continue
            x = int(e.get("timestamp") or 0)
            if x:
                w.pbook.x = x
                self.stats["p_lag"].append(lus * 1000 - x)
        if changed:
            self.stats["p_msgs"] += 1
            w.pbook.t = lus
            self.evaluate(w, lus, "pm")

    # ---------- Lighter ----------
    async def lighter(self):
        backoff = 1.0
        while not self.stop.is_set():
            try:
                async with websockets.connect(L_WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                    subbed, ping, backoff = set(), time.time(), 1.0
                    await ws.send(json.dumps({"type": "subscribe", "channel": "bo_market_stats/all"}))
                    while not self.stop.is_set():
                        want = {f"{k}/{mid}" for mid in self.by_mid for k in ("order_book", "trade")}
                        for c in sorted(want - subbed):
                            await ws.send(json.dumps({"type": "subscribe", "channel": c}))
                        for c in sorted(subbed - want):
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
                                self.on_lighter(raw)
                            except Exception as ex:  # noqa: BLE001
                                self.err("lighter_msg", ex, str(raw)[:200])
            except Exception as ex:  # noqa: BLE001
                self.err("lighter_ws", ex)
            self.stats["l_reconn"] += 1
            for w in self.win.values():
                w.lbook.clear()  # book unknown until the next snapshot
            await self.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30)

    def on_lighter(self, raw):
        lus = time.time()
        m = json.loads(raw)
        typ, ch = str(m.get("type", "")), str(m.get("channel", ""))
        if "order_book" in m and ch.startswith("order_book"):
            w = self.by_mid.get(int(re.split("[:/]", ch)[1]))
            if w is None:
                return
            if typ.startswith("subscribed"):
                w.lbook.clear()
            ob = m["order_book"]
            for side, key in ((0, "bids"), (1, "asks")):
                for lv in ob.get(key) or ():
                    w.lbook.set(side, px(lv["price"]), float(lv["size"]))
            x = int(m.get("timestamp") or 0)
            if x:
                w.lbook.x = x
                self.stats["l_lag"].append(lus * 1000 - x)
            w.lbook.t = lus
            self.stats["l_msgs"] += 1
            self.evaluate(w, lus, "lighter")
        elif ch.startswith("trade") and "trades" in m:
            w = self.by_mid.get(int(re.split("[:/]", ch)[1]))
            if w is None:
                return
            for t in m.get("trades") or ():
                # taker side: is_maker_ask -> the taker bought Up
                w.ltrades.append((lus, int(t.get("timestamp") or 0), px(t["price"]), float(t["size"]), bool(t.get("is_maker_ask"))))
            w.ltrades = [z for z in w.ltrades if z[0] > lus - 5]
        elif "bo_market_stats" in m:
            st = m["bo_market_stats"] or {}
            for s in [st] if "market_id" in st else st.values():
                if not isinstance(s, dict):
                    continue
                mid = int(s.get("market_id") or 0)
                if mid in self.known_mids and s.get("outcome") and mid not in self.l_outcomes:
                    self.l_outcomes[mid] = (str(s.get("outcome")), str(s.get("status")))

    # ---------- the frozen trigger ----------
    def fpm(self, w, p):
        return (w.pm_rate if w.pm_rate is not None else 0.05) * p * (1 - p)

    def evaluate(self, w, now, src):
        if w.up_token is None or now < w.e + START_OFFSET_S or now + (L_MATCH_MS + HEDGE_MS) / 1000 > w.end - GRID_S:
            return
        if not (w.lbook.valid() and w.pbook.valid()):
            return
        lb, la, lbs, las = w.lbook.best()
        pb, pa, pbs, pas = w.pbook.best()
        ea = 1 - (la + (1 - pb) + fl(la) + self.fpm(w, 1 - pb))
        eb = 1 - ((1 - lb) + pa + fl(lb) + self.fpm(w, pa))
        for side, edge, lpx, lsz, hpx, hsz in (("a", ea, la, las, 1 - pb, pbs), ("b", eb, lb, lbs, pa, pas)):
            if edge > TH and min(lsz, hsz) >= SIZE and now >= w.nxt[side]:
                w.nxt[side] = now + L_MATCH_MS / 1000
                w.n_trig += 1
                self.stats["triggers"] += 1
                tid = f"{w.e}-{side}-{int(now * 1000)}"
                pair_cost = lpx + hpx + fl(lpx) + self.fpm(w, hpx) if side == "a" else (1 - lpx) + hpx + fl(lpx) + self.fpm(w, hpx)
                self.log("trigger", id=tid, src=src, window=w.e, side=side, t_exch_lighter=w.lbook.x, t_exch_pm=w.pbook.x,
                         lighter_px=lpx, lighter_size=lsz, pm_px=round(hpx, 4), pm_size=hsz, pm_book=[pb, pa, pbs, pas],
                         lighter_book=[lb, la, lbs, las], pair_cost_after_fees=round(pair_cost, 5), edge=round(edge, 5),
                         pm_fee_rate=w.pm_rate, book_age_ms=[round((now - w.lbook.t) * 1000, 1), round((now - w.pbook.t) * 1000, 1)])
                loop = asyncio.get_running_loop()
                for ms in (100, 300, 500):
                    loop.call_later(ms / 1000, self.check, w, tid, side, lpx, now, ms)

    def lquote_there(self, w, side, lpx):
        if not w.lbook.valid():
            return False, None, None
        lb, la, lbs, las = w.lbook.best()
        p, s = (la, las) if side == "a" else (lb, lbs)
        ok = s >= SIZE and (p <= lpx + 1e-9 if side == "a" else p >= lpx - 1e-9)
        return ok, p, s

    def check(self, w, tid, side, lpx, t0, ms):
        try:
            ok, p, s = self.lquote_there(w, side, lpx)
            now = time.time()
            raced = [z for z in w.ltrades if t0 <= z[0] <= now and z[4] == (side == "a")]
            self.log("check", id=tid, window=w.e, side=side, at_ms=ms, late_ms=round((now - t0) * 1000 - ms, 1), displayed=ok,
                     lighter_px=p, lighter_size=s, t_exch_lighter=w.lbook.x, raced_so_far=len(raced))
            if ms == 500:
                if ok:
                    w.nxt[side] = t0 + L_MATCH_MS / 1000 + COOL_S
                    asyncio.get_running_loop().call_later(HEDGE_MS / 1000, self.hedge, w, tid, side, p, s, t0)
        except Exception as ex:  # noqa: BLE001
            self.err("check", ex)

    def hedge(self, w, tid, side, lfill, lsz, t0):
        try:
            now = time.time()
            if not w.pbook.valid():
                self.log("hedge", id=tid, window=w.e, side=side, hedged=False, reason="pm book unknown")
                return
            pb, pa, pbs, pas = w.pbook.best()
            hp, hs = ((1 - pb), pbs) if side == "a" else (pa, pas)
            qty = min(float(lsz), float(hs), CAP)
            if qty < SIZE:
                self.log("hedge", id=tid, window=w.e, side=side, hedged=False, reason="hedge size < 5", pm_px=round(hp, 4), pm_size=hs)
                return
            pnl = (1 - lfill - hp if side == "a" else lfill - hp) - fl(lfill) - self.fpm(w, hp)
            w.pairs.append(dict(id=tid, side=side, qty=qty, lfill=lfill, hp=hp, pnl=pnl))
            self.log("hedge", id=tid, window=w.e, side=side, hedged=True, late_ms=round((now - t0) * 1000 - 1000, 1), lighter_fill_px=lfill,
                     pm_px=round(hp, 4), pm_size=hs, qty=qty, pnl_per_share=round(pnl, 5), pnl_usd=round(pnl * qty, 4),
                     t_exch_pm=w.pbook.x, pm_book=[pb, pa, pbs, pas])
        except Exception as ex:  # noqa: BLE001
            self.err("hedge", ex)

    async def settle(self, w):
        """Wait for both venues' outcomes, then log the paper P&L of the window's pairs."""
        pm_w = l_out = None
        for _ in range(40):
            await self.sleep(30)
            try:
                if pm_w is None and w.slug:
                    m = (await asyncio.to_thread(http_json, GAMMA + f"slug={w.slug}"))[0]
                    names = [n.upper() for n in json.loads(m["outcomes"])]
                    pr = [float(v) for v in json.loads(m["outcomePrices"])]
                    if max(pr) >= 0.99:
                        pm_w = names[pr.index(max(pr))]
                if l_out is None and w.mid in self.l_outcomes:
                    l_out = self.l_outcomes[w.mid]
            except Exception as ex:  # noqa: BLE001
                self.err("settle", ex)
            if pm_w is not None and (l_out is not None or not w.pairs):
                break
        # a hedged pair pays $1 whichever side wins when both venues settle the same way
        self.known_mids.discard(w.mid)
        self.l_outcomes.pop(w.mid, None)
        usd = sum(p["pnl"] * p["qty"] for p in w.pairs)
        sh = sum(p["qty"] for p in w.pairs)
        self.log("settle", window=w.e, pm_winner=pm_w, lighter_outcome=l_out, triggers=w.n_trig, pairs=len(w.pairs), shares=sh,
                 paper_pnl_usd=round(usd, 4), cents_per_share=round(100 * usd / sh, 3) if sh else None,
                 note="lighter outcome 2 = Up, 1 = Down (holdout map); P&L assumes both venues agree")

    # ---------- housekeeping ----------
    async def health(self):
        while not self.stop.is_set():
            await self.sleep(60)
            s = self.stats

            def pct(v):
                v = sorted(v)
                return [round(v[int(len(v) * q)], 1) for q in (0.1, 0.5, 0.9)] if v else None

            self.log("health", windows=sorted(self.win), l_msgs=s["l_msgs"], p_msgs=s["p_msgs"], l_reconn=s["l_reconn"], p_reconn=s["p_reconn"],
                     l_lag_ms=pct(s["l_lag"]), p_lag_ms=pct(s["p_lag"]), errors=s["errors"], triggers=s["triggers"],
                     rss_mb=round(int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20, 1))
            s["l_lag"].clear()
            s["p_lag"].clear()

    def err(self, where, ex, raw=None):
        self.stats["errors"] += 1
        self.log("error", where=where, err=repr(ex)[:300], raw=raw)

    async def sleep(self, s):
        try:
            await asyncio.wait_for(self.stop.wait(), s)
        except asyncio.TimeoutError:
            pass

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        if self.stop_at:
            loop.call_later(self.stop_at - time.time(), self.stop.set)
        self.log("start", pid=os.getpid(), mode="paper", rule="frozen/t1_lighter_pm_arb.json", th=TH, python=sys.version.split()[0])
        tasks = [asyncio.create_task(c) for c in (self.discover(), self.lighter(), self.health())]
        await self.stop.wait()
        self.log("stop", pending_windows=sorted(self.win))
        for t in tasks:
            t.cancel()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes (foreground test)")
    a = ap.parse_args()
    p = Paper(Log(a.out), time.time() + a.minutes * 60 if a.minutes else None)
    asyncio.run(p.run())


if __name__ == "__main__":
    main()
