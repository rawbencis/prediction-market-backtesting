"""Shadow recorder: Polymarket crypto up/down books (5m, 15m) + Binance trades. Read-only: no keys, no orders.

rec:   Binance spot and USD-M aggTrade for BTC/ETH/SOL/XRP/DOGE, and the UP token's top of book for
       every live 5m / 15m window (DOWN is the mirror: ask = 1 - UP bid). Polymarket sockets opened 45 s
       before each window starts: by default one per token (--split), since one socket per window
       backlogged by seconds over Tokyo<->London. Fixed-width binary rows, hourly files under OUT/YYYY-MM-DD/.
check: print the markets found for the current windows and the CLOB market info (taker delay, fees).

pm row: local_us, exchange_ms, asset, tf, epoch, bid, ask (1e-4 $), bid_size, ask_size. bid=ask=0 marks
        a dropped connection (book unknown from there until the next row).
bn row: local_us, trade_ms, src (0 spot, 1 USD-M), asset, price.
tr row: local_us, exchange_ms, asset, tf, epoch, token (0 UP, 1 DOWN), side (0 BUY, 1 SELL: taker side),
        price (1e-4 $), size, fee_rate_bps, transaction hash (32 bytes). From `last_trade_price`; both tokens
        are subscribed for this (books are still kept for UP only). `--no-trades` turns it off.
"""

import argparse
import asyncio
import json
import shutil
import struct
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import orjson

    loads = orjson.loads
except ImportError:
    loads = json.loads

websockets = None  # imported in main, so the evaluator can import this module without it

ASSETS = ("btc", "eth", "sol", "xrp", "doge")
TFS = (5, 15)
PM_REC = struct.Struct("<qqBBIHHff")
BN_REC = struct.Struct("<qqBBd")
TR_REC = struct.Struct("<qqBBIBBHfH32s")
GAMMA = "https://gamma-api.polymarket.com/markets?"
CLOB = "https://clob.polymarket.com/clob-markets/"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BN_WS = (
    "wss://stream.binance.com:9443/stream?streams=",
    "wss://fstream.binance.com/market/stream?streams=",
)
LEAD_S = 45


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "updown-shadow"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def slug(asset, tf, epoch):
    return f"{asset}-updown-{tf}m-{epoch}"


def find_markets(tf, epoch, downs=None):
    """-> {up_token: (asset_idx, slug, condition_id)} for the tf-minute window starting at epoch.
    If `downs` is a dict it is filled with {up_token: down_token}."""
    want = {slug(a, tf, epoch): i for i, a in enumerate(ASSETS)}
    out = {}
    for m in http_json(GAMMA + "&".join(f"slug={s}" for s in want)):
        try:
            names = [n.upper() for n in json.loads(m["outcomes"])]
            toks = json.loads(m["clobTokenIds"])
            out[toks[names.index("UP")]] = (want[m["slug"]], m["slug"], m.get("conditionId", ""))
            if downs is not None:
                downs[toks[names.index("UP")]] = toks[names.index("DOWN")]
        except (KeyError, ValueError, TypeError):
            continue
    return out


class Out:
    def __init__(self, root):
        self.root, self.buf, self.n = root, {"pm": [], "bn": [], "tr": []}, {"pm": 0, "bn": 0, "tr": 0}

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


def px(s):
    return int(round(float(s) * 10000))


def tx_bytes(h):
    try:
        return bytes.fromhex((h or "")[2:] if (h or "").startswith("0x") else (h or ""))[:32].ljust(32, b"\0")
    except ValueError:
        return b"\0" * 32


def on_msg(raw, lus, toks, tf, epoch, books, last, out, lag=None, trk=None):
    """Apply one market-channel message; write a row for each token whose top of book changed, and
    (if trk = {token: (asset_idx, 0 UP / 1 DOWN)}) a tr row per last_trade_price.
    -> latest exchange ms seen in book events (0 if none)."""
    ev = loads(raw)
    xmax = 0
    for e in ev if isinstance(ev, list) else (ev,):
        et = e.get("event_type")
        if et == "last_trade_price":
            t = trk.get(e.get("asset_id")) if trk else None
            if t:
                try:
                    out.add("tr", TR_REC.pack(lus, int(e.get("timestamp") or 0), t[0], tf, epoch, t[1],
                                              0 if e.get("side") == "BUY" else 1, px(e["price"]), float(e["size"]),
                                              int(e.get("fee_rate_bps") or 0), tx_bytes(e.get("transaction_hash"))))  # fmt: skip
                except (KeyError, TypeError, ValueError):
                    pass
            continue
        if et == "book":
            a = e.get("asset_id")
            if a not in books:
                continue
            books[a] = tuple(
                {px(v["price"]): float(v["size"]) for v in e.get(s) or () if float(v["size"]) > 0}
                for s in ("bids", "asks")
            )
            touched = (a,)
        elif et == "price_change":
            ch = e.get("price_changes")
            if ch is None:  # pre-2025 shape
                ch = [dict(c, asset_id=e.get("asset_id")) for c in e.get("changes") or ()]
            touched = set()
            for c in ch:
                a = c.get("asset_id")
                if a not in books:
                    continue
                side = books[a][0 if c["side"] == "BUY" else 1]
                p, s = px(c["price"]), float(c["size"])
                if s > 0:
                    side[p] = s
                else:
                    side.pop(p, None)
                touched.add(a)
        else:
            continue
        x = int(e.get("timestamp") or 0)
        xmax = max(xmax, x)
        for a in touched:
            b, k = books[a]
            bb, ba = (max(b) if b else 0), (min(k) if k else 0)
            top = (bb, ba, b.get(bb, 0.0), k.get(ba, 0.0))
            if top != last.get(a):
                last[a] = top
                out.add("pm", PM_REC.pack(lus, x, toks[a][0], tf, epoch, *top))
                if lag is not None and x:
                    lag.append(lus / 1000 - x)
    return xmax


async def pm_window(tf, epoch, out, st, trades=True, split="token"):
    end = epoch + tf * 60 + 3
    toks, downs, deadline = {}, {}, max(epoch + 60, time.time() + 15)
    while time.time() < deadline:
        try:
            downs = {}
            toks = await asyncio.to_thread(find_markets, tf, epoch, downs)
        except Exception as e:  # noqa: BLE001
            st["err"] = f"gamma {tf}m {epoch}: {e!r}"[:160]
        # want all assets; settle for a partial set only once the open is about to pass
        if len(toks) == len(ASSETS) or (toks and time.time() >= epoch - 5):
            break
        await asyncio.sleep(3)
    if not toks:
        st["nomkt"] += 1
        return
    with (out.root / "markets.csv").open("a") as f:
        for t, (i, sl, cond) in toks.items():
            f.write(f"{tf},{epoch},{ASSETS[i]},{sl},{cond},{t}\n")
    trk = None
    if trades:
        trk = {t: (i, 0) for t, (i, _, _) in toks.items()}
        trk.update({downs[t]: (toks[t][0], 1) for t in toks if t in downs})
        with (out.root / "markets_down.csv").open("a") as f:
            for t in toks:
                if t in downs:
                    f.write(f"{tf},{epoch},{ASSETS[toks[t][0]]},{t},{downs[t]}\n")
    if tf == 5 and epoch % 3600 == 0:
        try:
            cond = next(c for i, _, c in toks.values() if i == 0)
            info = await asyncio.to_thread(http_json, CLOB + cond)
            print("clob-market", slug("btc", tf, epoch), json.dumps(info)[:600], flush=True)
        except Exception as e:  # noqa: BLE001
            st["err"] = f"clob info: {e!r}"[:160]
    # one socket per group: a single Tokyo<->London TCP stream can't carry every book's update rate, and the
    # backlog grows for as long as the socket lives (2026-11-14: p90 8.7 s on one socket per window)
    groups = []
    for t, (i, _, _) in toks.items():
        up_trk = {t: (i, 0)} if trk else None
        dn = {downs[t]: (i, 1)} if trk and t in downs else {}
        if split == "window":
            continue
        if split == "asset":
            groups.append(({t: toks[t]}, {**(up_trk or {}), **dn} or None))
        else:  # token: UP books (+ UP trades) on one socket, DOWN trades on another
            groups.append(({t: toks[t]}, up_trk))
            if dn:
                groups.append(({}, dn))
    if split == "window":
        groups = [(toks, trk)]
    await asyncio.gather(*(pm_conn(tf, epoch, end, g, k, out, st) for g, k in groups))


async def pm_conn(tf, epoch, end, toks, trk, out, st):
    subs = list(trk or toks)
    while time.time() < end:
        books, last, xl, up = {t: ({}, {}) for t in toks}, {}, 0, False
        try:
            async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": subs, "type": "market"}))
                st["conn"] += 1
                up, nxt = True, time.time() + 10
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        raw = None
                    lus = time.time_ns() // 1000
                    if lus / 1e6 >= nxt:
                        await ws.send("PING")
                        nxt = lus / 1e6 + 10
                    if raw and raw not in ("PONG", b"PONG"):
                        xl = max(xl, on_msg(raw, lus, toks, tf, epoch, books, last, out, st["lag"], trk))
        except Exception as e:  # noqa: BLE001
            st["err"] = f"pm {tf}m {epoch}: {e!r}"[:160]
        if up:
            st["conn"] -= 1
        if time.time() < end and xl:  # dropped mid-window: mark the books unknown
            st["drops"] += 1
            lus = time.time_ns() // 1000
            for t in last:
                out.add("pm", PM_REC.pack(lus, xl + 1, toks[t][0], tf, epoch, 0, 0, 0.0, 0.0))
            await asyncio.sleep(0.5)
        elif time.time() < end and not up:
            await asyncio.sleep(1)


async def pm_sched(out, st, trades=True, split="token"):
    seen, tasks = set(), set()
    while True:
        now = time.time()
        for tf in TFS:
            w = tf * 60
            cur = int(now // w) * w
            for e in (cur, cur + w):
                if (tf, e) not in seen and now >= e - LEAD_S:
                    seen.add((tf, e))
                    t = asyncio.create_task(pm_window(tf, e, out, st, trades, split))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        seen = {k for k in seen if k[1] > now - 7200}
        await asyncio.sleep(1)


async def binance(src, out, st):
    url = BN_WS[src] + "/".join(f"{a}usdt@aggTrade" for a in ASSETS)
    idx = {f"{a}usdt".upper(): i for i, a in enumerate(ASSETS)}
    while True:
        try:
            async with websockets.connect(url, open_timeout=10, max_size=None) as ws:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), 30)
                    lus = time.time_ns() // 1000
                    d = loads(raw)["data"]
                    out.add("bn", BN_REC.pack(lus, d["T"], src, idx[d["s"]], float(d["p"])))
                    st["bl"][src].append(lus / 1000 - d["T"])
        except Exception as e:  # noqa: BLE001
            st["err"] = f"binance{src}: {e!r}"[:160]
            st["bdrops"] += 1
        await asyncio.sleep(1)


def pct(v):
    v = sorted(v)
    return [round(v[int(len(v) * q)]) for q in (0.1, 0.5, 0.9)] if v else None


def prune(root, keep):
    cut = (datetime.now(timezone.utc) - timedelta(days=keep)).date().isoformat()
    for d in root.glob("????-??-??"):
        if d.is_dir() and d.name < cut:
            shutil.rmtree(d)
            print("pruned", d.name, flush=True)


async def housekeeping(out, st, keep):
    k = 0
    while True:
        await asyncio.sleep(1)
        out.flush()
        k += 1
        if k % 60 == 0:
            print(
                f"rows pm={out.n['pm']} bn={out.n['bn']} tr={out.n['tr']} conns={st['conn']} pm_lag_ms={pct(st['lag'])} "
                f"spot_lag={pct(st['bl'][0])} fut_lag={pct(st['bl'][1])} drops={st['drops']}/{st['bdrops']} "
                f"no_market={st['nomkt']} last_err={st['err']}",
                flush=True,
            )
            st["lag"].clear()
            st["bl"][0].clear()
            st["bl"][1].clear()
            st["err"] = None
        if k % 3600 == 30:
            prune(out.root, keep)


async def run(a):
    a.out.mkdir(parents=True, exist_ok=True)
    out = Out(a.out)
    st = {"lag": [], "bl": ([], []), "conn": 0, "drops": 0, "bdrops": 0, "nomkt": 0, "err": None}
    await asyncio.gather(
        binance(0, out, st), binance(1, out, st), pm_sched(out, st, not a.no_trades, a.split), housekeeping(out, st, a.keep_days)
    )


def cmd_check(_):
    now = int(time.time())
    for tf in TFS:
        e = now // (tf * 60) * (tf * 60)
        found = find_markets(tf, e)
        print(f"{tf}m window {e}: {len(found)}/{len(ASSETS)} markets")
        for t, (i, sl, cond) in sorted(found.items(), key=lambda kv: kv[1][0]):
            print(f"  {sl}  up_token=...{t[-8:]}  condition={cond[:12]}")
        for i, sl, cond in found.values():
            if i == 0:
                print("  clob-market info:", json.dumps(http_json(CLOB + cond))[:900])


def main():
    global websockets
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("rec")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--keep-days", type=int, default=14, help="delete recorded day folders older than this")
    p.add_argument("--no-trades", action="store_true", help="do not subscribe DOWN tokens / record trade prints")
    p.add_argument("--split", choices=("token", "asset", "window"), default="token",
                   help="websocket per token (default), per asset, or one per window (pre-2026-11-14 behaviour)")
    sp.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "check":
        return cmd_check(a)
    import websockets

    asyncio.run(run(a))


if __name__ == "__main__":
    main()
