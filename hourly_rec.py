"""Hourly / 4-hour up/down recorder (Dublin): BTC and ETH `<asset>-updown-1h-<epoch>` and `-4h-<epoch>` books,
prints, top-5 depth, REST re-snapshots and the official resolution values. Read-only: no keys, no orders.

  python hourly_rec.py --out ~/hourly-rec --keep-days 60            (systemd: hourly-rec, deploy/hourly-rec.service)
  python hourly_rec.py --out /tmp/x --keep-days 1 --minutes 70       (foreground test: stop after 70 min, print summary)

Windows. Every hour boundary h, from T-10 min, Gamma is asked for btc/eth-updown-1h-h and btc/eth-updown-4h-h. The 4h
series starts at 00/04/08/12/16/20 ET (13:00, 17:00, 21:00 ... UTC in winter), so it is probed at every hour and found
only where it exists. A window is subscribed (both tokens, one socket per window) from discovery to close + 5 min.

Files: OUT/YYYY-MM-DD/<kind>-HH.bin, by local UTC time of writing; OUT/markets.csv; OUT/YYYY-MM-DD/resolutions.jsonl.
Asset index: 0 btc, 1 eth (same as shadow_rec.ASSETS). tf is in minutes (60 or 240). Prices are 1e-4 $ integers.

pm row (PM_REC, identical to shadow_rec.py): local_us q, exchange_ms q, asset B, tf B, epoch I, bid H, ask H,
        bid_size f, ask_size f. UP token only (DOWN is the mirror). bid=ask=0: connection dropped, book unknown
        until the next row.
tr row (TR_REC, identical to shadow_rec.py): local_us q, exchange_ms q, asset B, tf B, epoch I, token B (0 UP, 1 DOWN),
        side B (0 BUY, 1 SELL; taker side), price H, size f, fee_rate_bps H, tx hash 32s. From last_trade_price.
bk row (BK_REC): local_us q, exchange_ms q, asset B, tf B, epoch I, token B, nb B, na B, then 5 bid levels
        (price H, size f) best first, then 5 ask levels, zero-padded. Sampled every 1 s per token; written only when
        the top 5 changed since the last bk row of that token, or 10 s have passed (heartbeat), so a reader
        forward-fills. exchange_ms is the last book event applied.
rs row (BK_REC layout): the same fields from a REST GET /book?token_id= every 60 s per token; exchange_ms is the REST
        book's own timestamp. The log counts snapshots whose best bid/ask differ from the websocket book.
resolutions.jsonl: one JSON object per window once Gamma shows it resolved (eventMetadata.finalPrice present):
        slug, asset, tf, epoch, priceToBeat, finalPrice, outcomePrices, umaResolutionStatus, closedTime,
        resolutionSource, description (the resolution text), cryptoMarketConfig, feeSchedule, conditionId, tokens.
"""

import argparse
import asyncio
import gzip
import json
import resource
import shutil
import struct
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orjson
import websockets

ASSETS = ("btc", "eth")
SERIES = (("1h", 60), ("4h", 240))
PM_REC = struct.Struct("<qqBBIHHff")
TR_REC = struct.Struct("<qqBBIBBHfH32s")
BK_REC = struct.Struct("<qqBBIBBB" + "Hf" * 10)
GAMMA = "https://gamma-api.polymarket.com/"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id="
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LEAD_S, TAIL_S, DEPTH = 600, 300, 5
HDR = {"User-Agent": "updown-shadow", "Accept-Encoding": "gzip"}


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), *a, flush=True)


def http_json(url):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=10) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            b = gzip.decompress(b)
    return orjson.loads(b)


def px(s):
    return int(round(float(s) * 10000))


def tx_bytes(h):
    h = h or ""
    try:
        return bytes.fromhex(h[2:] if h.startswith("0x") else h)[:32].ljust(32, b"\0")
    except ValueError:
        return b"\0" * 32


class Out:
    def __init__(self, root):
        self.root = root
        self.buf = {k: [] for k in ("pm", "tr", "bk", "rs")}
        self.n = {k: 0 for k in self.buf}
        self.bytes = 0

    def add(self, kind, row):
        self.buf[kind].append(row)

    def flush(self):
        t = time.gmtime()
        d = self.root / time.strftime("%Y-%m-%d", t)
        d.mkdir(parents=True, exist_ok=True)
        for k, b in self.buf.items():
            if b:
                data = b"".join(b)
                with (d / f"{k}-{t.tm_hour:02d}.bin").open("ab") as f:
                    f.write(data)
                self.n[k] += len(b)
                self.bytes += len(data)
                b.clear()

    def jsonl(self, name, obj):
        d = self.root / time.strftime("%Y-%m-%d", time.gmtime())
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("ab") as f:
            f.write(orjson.dumps(obj) + b"\n")


def levels(book, n=DEPTH):
    b, a = book
    bb = sorted(b, reverse=True)[:n]
    aa = sorted(a)[:n]
    return [(p, b[p]) for p in bb], [(p, a[p]) for p in aa]


def bk_pack(lus, x, ai, tf, epoch, tok, bl, al):
    flat = []
    for lv in (bl, al):
        for i in range(DEPTH):
            flat += list(lv[i]) if i < len(lv) else [0, 0.0]
    return BK_REC.pack(lus, x, ai, tf, epoch, tok, len(bl), len(al), *flat)


class Window:
    def __init__(self, series, tf, epoch, ai, slug, m):
        self.series, self.tf, self.epoch, self.ai, self.slug = series, tf, epoch, ai, slug
        names = [n.lower() for n in json.loads(m["outcomes"])]
        ids = json.loads(m["clobTokenIds"])
        self.tok = {ids[names.index("up")]: 0, ids[names.index("down")]: 1}
        self.cond = m.get("conditionId", "")
        self.end = epoch + tf * 60
        self.books = {t: ({}, {}) for t in self.tok}
        self.xlast = {t: 0 for t in self.tok}
        self.top = {}
        self.bk_last = {}
        self.up = False


def on_msg(raw, lus, w, out, st):
    ev = orjson.loads(raw)
    for e in ev if isinstance(ev, list) else (ev,):
        et = e.get("event_type")
        x = int(e.get("timestamp") or 0)
        if et == "last_trade_price":
            t = w.tok.get(e.get("asset_id"))
            if t is not None:
                try:
                    out.add("tr", TR_REC.pack(lus, x, w.ai, w.tf, w.epoch, t, 0 if e.get("side") == "BUY" else 1,
                                              px(e["price"]), float(e["size"]), int(e.get("fee_rate_bps") or 0),
                                              tx_bytes(e.get("transaction_hash"))))  # fmt: skip
                except (KeyError, TypeError, ValueError):
                    st["bad"] += 1
            continue
        if et == "book":
            a = e.get("asset_id")
            if a not in w.books:
                continue
            w.books[a] = tuple({px(v["price"]): float(v["size"]) for v in e.get(s) or () if float(v["size"]) > 0}
                               for s in ("bids", "asks"))  # fmt: skip
            touched = (a,)
        elif et == "price_change":
            ch = e.get("price_changes")
            if ch is None:
                ch = [dict(c, asset_id=e.get("asset_id")) for c in e.get("changes") or ()]
            touched = set()
            for c in ch:
                a = c.get("asset_id")
                if a not in w.books:
                    continue
                side = w.books[a][0 if c["side"] == "BUY" else 1]
                p, s = px(c["price"]), float(c["size"])
                if s > 0:
                    side[p] = s
                else:
                    side.pop(p, None)
                touched.add(a)
        else:
            continue
        for a in touched:
            w.xlast[a] = max(w.xlast[a], x)
            if w.tok[a] != 0:
                continue
            b, k = w.books[a]
            bb, ba = (max(b) if b else 0), (min(k) if k else 0)
            top = (bb, ba, b.get(bb, 0.0), k.get(ba, 0.0))
            if top != w.top.get(a):
                w.top[a] = top
                out.add("pm", PM_REC.pack(lus, x, w.ai, w.tf, w.epoch, *top))
                if x:
                    st["lag"].append(lus / 1000 - x)


async def ws_loop(w, out, st):
    drop_at = w.end + TAIL_S
    while time.time() < drop_at:
        w.books = {t: ({}, {}) for t in w.tok}
        try:
            async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": list(w.tok), "type": "market"}))
                st["conn"] += 1
                st["connects"] += 1
                w.up, nxt = True, time.time() + 10
                while time.time() < drop_at:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        raw = None
                    lus = time.time_ns() // 1000
                    if lus / 1e6 >= nxt:
                        await ws.send("PING")
                        nxt = lus / 1e6 + 10
                    if raw and raw not in ("PONG", b"PONG"):
                        on_msg(raw, lus, w, out, st)
        except Exception as e:  # noqa: BLE001
            st["err"] = f"ws {w.slug}: {e!r}"[:160]
            if time.time() >= w.end and "resolved" in repr(e):  # server ends the feed ~2 min after close: done
                drop_at = 0
        if w.up:
            st["conn"] -= 1
        w.up = False
        if time.time() < drop_at:
            st["reconnects"] += 1
            lus = time.time_ns() // 1000
            for a in w.tok:
                if w.tok[a] == 0:
                    out.add("pm", PM_REC.pack(lus, w.xlast[a] + 1, w.ai, w.tf, w.epoch, 0, 0, 0.0, 0.0))
                    w.top.pop(a, None)
            await asyncio.sleep(1)


async def depth_loop(w, out):
    drop_at = w.end + TAIL_S
    while time.time() < drop_at:
        await asyncio.sleep(1 - (time.time() % 1))
        if not w.up:
            continue
        lus = time.time_ns() // 1000
        for a, t in w.tok.items():
            if not w.xlast[a]:
                continue
            bl, al = levels(w.books[a])
            last = w.bk_last.get(a)
            if last is None or last[1] != (bl, al) or lus - last[0] >= 10_000_000:
                w.bk_last[a] = (lus, (bl, al))
                out.add("bk", bk_pack(lus, w.xlast[a], w.ai, w.tf, w.epoch, t, bl, al))


async def rest_loop(w, out, st):
    drop_at = w.end + TAIL_S
    await asyncio.sleep(5)
    while time.time() < drop_at:
        for a, t in w.tok.items():
            try:
                r = await asyncio.to_thread(http_json, CLOB_BOOK + a)
                lus = time.time_ns() // 1000
                b = {px(v["price"]): float(v["size"]) for v in r.get("bids") or () if float(v["size"]) > 0}
                k = {px(v["price"]): float(v["size"]) for v in r.get("asks") or () if float(v["size"]) > 0}
                bl, al = levels((b, k))
                out.add("rs", bk_pack(lus, int(r.get("timestamp") or 0), w.ai, w.tf, w.epoch, t, bl, al))
                st["rest"] += 1
                if w.up and w.xlast[a]:
                    wb, wa = levels(w.books[a], 1)
                    if (wb[:1], wa[:1]) != (bl[:1], al[:1]):
                        st["rest_mismatch"] += 1
            except Exception as e:  # noqa: BLE001
                if time.time() >= w.end and "404" in repr(e):  # book removed after resolution: stop polling
                    return
                st["err"] = f"rest {w.slug}: {e!r}"[:160]
                st["rest_err"] += 1
            await asyncio.sleep(0.5)
        await asyncio.sleep(60 - len(w.tok) * 0.5)


async def resolve(w, out, st):
    """After close, poll Gamma events until eventMetadata.finalPrice is there (every 2 min, up to 3 h)."""
    await asyncio.sleep(max(0, w.end + 60 - time.time()))
    for _ in range(90):
        try:
            ev = await asyncio.to_thread(http_json, GAMMA + "events?slug=" + w.slug)
            if ev:
                e = ev[0]
                md = e.get("eventMetadata") or {}
                m = (e.get("markets") or [{}])[0]
                if md.get("finalPrice") is not None and m.get("closed"):
                    out.jsonl("resolutions.jsonl", {
                        "slug": w.slug, "asset": ASSETS[w.ai], "tf": w.tf, "epoch": w.epoch,
                        "priceToBeat": md.get("priceToBeat"), "finalPrice": md.get("finalPrice"),
                        "outcomePrices": m.get("outcomePrices"), "umaResolutionStatus": m.get("umaResolutionStatus"),
                        "closedTime": m.get("closedTime") or e.get("closedTime"),
                        "resolutionSource": m.get("resolutionSource") or e.get("resolutionSource"),
                        "description": m.get("description") or e.get("description"),
                        "cryptoMarketConfig": m.get("cryptoMarketConfig"), "feeSchedule": m.get("feeSchedule"),
                        "conditionId": w.cond, "tokens": {str(v): k for k, v in w.tok.items()},
                        "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})  # fmt: skip
                    st["resolved"] += 1
                    return
        except Exception as e:  # noqa: BLE001
            st["err"] = f"resolve {w.slug}: {e!r}"[:160]
        await asyncio.sleep(120)
    st["unresolved"] += 1
    log("no resolution after 3 h", w.slug)


async def run_window(w, out, st):
    with (out.root / "markets.csv").open("a") as f:
        ids = sorted(w.tok, key=w.tok.get)
        f.write(f"{w.tf},{w.epoch},{ASSETS[w.ai]},{w.slug},{w.cond},{ids[0]},{ids[1]}\n")
    st["windows"] += 1
    log("window", w.slug, "until", time.strftime("%H:%M", time.gmtime(w.end + TAIL_S)))
    await asyncio.gather(ws_loop(w, out, st), depth_loop(w, out), rest_loop(w, out, st))
    await resolve(w, out, st)


async def scheduler(out, st):
    seen, tasks = set(), set()
    while True:
        now = time.time()
        h = int(now // 3600) * 3600
        want = []
        for e in range(h - 4 * 3600, h + 3601, 3600):
            for series, tf in SERIES:
                if now < e - LEAD_S or now >= e + tf * 60:
                    continue
                for ai, a in enumerate(ASSETS):
                    sl = f"{a}-updown-{series}-{e}"
                    if sl not in seen:
                        want.append((sl, series, tf, e, ai))
        if want:
            try:
                ms = await asyncio.to_thread(http_json, GAMMA + "markets?" + "&".join(f"slug={x[0]}" for x in want))
                got = {m["slug"]: m for m in ms}
                for sl, series, tf, e, ai in want:
                    if sl in got:
                        seen.add(sl)
                        t = asyncio.create_task(run_window(Window(series, tf, e, ai, sl, got[sl]), out, st))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                    elif series == "4h" or now >= e + 120:
                        seen.add(sl)  # 4h exists only at its own boundaries; 1h missing 2 min after open: give up
                        if series == "1h":
                            st["nomkt"] += 1
                            log("no market", sl)
            except Exception as e:  # noqa: BLE001
                st["err"] = f"gamma: {e!r}"[:160]
                st["gamma_err"] += 1
        seen = {s for s in seen if int(s.rsplit("-", 1)[1]) > now - 6 * 3600}
        await asyncio.sleep(30)


def pct(v):
    v = sorted(v)
    return [round(v[int(len(v) * q)]) for q in (0.1, 0.5, 0.9)] if v else None


def prune(root, keep):
    cut = (datetime.now(timezone.utc) - timedelta(days=keep)).date().isoformat()
    for d in root.glob("????-??-??"):
        if d.is_dir() and d.name < cut:
            shutil.rmtree(d)
            log("pruned", d.name)


async def housekeeping(out, st, keep, t_stop):
    k = 0
    prune(out.root, keep)
    while True:
        await asyncio.sleep(1)
        out.flush()
        k += 1
        if k % 60 == 0:
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            log(f"rows {out.n} MB={out.bytes / 1e6:.2f} conns={st['conn']} connects={st['connects']} "
                f"reconnects={st['reconnects']} windows={st['windows']} resolved={st['resolved']} "
                f"rest={st['rest']} rest_mismatch={st['rest_mismatch']} rest_err={st['rest_err']} "
                f"gamma_err={st['gamma_err']} no_market={st['nomkt']} lag_ms={pct(st['lag'])} maxrss_MB={rss} "
                f"last_err={st['err']}")  # fmt: skip
            st["lag"].clear()
            st["err"] = None
        if k % 3600 == 1800:
            prune(out.root, keep)
        if t_stop and time.time() >= t_stop:
            out.flush()
            return


async def run(a):
    a.out.mkdir(parents=True, exist_ok=True)
    out = Out(a.out)
    st = dict(lag=[], conn=0, connects=0, reconnects=0, windows=0, resolved=0, unresolved=0, rest=0, rest_mismatch=0,
              rest_err=0, gamma_err=0, nomkt=0, bad=0, err=None)  # fmt: skip
    t0 = time.time()
    t_stop = t0 + a.minutes * 60 if a.minutes else None
    hk = asyncio.create_task(housekeeping(out, st, a.keep_days, t_stop))
    sc = asyncio.create_task(scheduler(out, st))
    await hk
    sc.cancel()
    hrs = (time.time() - t0) / 3600
    log(f"SUMMARY hours={hrs:.2f} rows={out.n} MB={out.bytes / 1e6:.2f} MB/h={out.bytes / 1e6 / hrs:.2f} "
        f"proj_GB/day={out.bytes / 1e9 / hrs * 24:.3f} {({k: v for k, v in st.items() if k not in ('lag', 'err')})}")  # fmt: skip


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path.home() / "hourly-rec")
    ap.add_argument("--keep-days", type=int, default=60, help="delete day folders older than this")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this many minutes (test mode)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
