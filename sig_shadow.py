"""Octopus signal-latency shadow: when does THIS box first see a watched wallet's Polymarket fill?

Read-only, no orders, no Polymarket keys. Two feeds, both logged per fill:
  alch  Alchemy Polygon `logs` subscription on the CTF exchanges, the same two filters tentacle_ws uses
        (watched wallet as order owner = topics[2], as counterparty = topics[3]). Needs ALCHEMY_API_KEY.
  rtds  Polymarket RTDS `activity/trades` firehose (wss://ws-live-data.polymarket.com), kept when the
        trade's proxy wallet is watched. Every minute the log line also prints RTDS's own lag
        (receipt - message timestamp) over ALL trades, to spot a long-pipe backlog like the market channel's.

run      record to OUT/YYYY-MM-DD.csv (one row per received fill, both feeds)
wallets  print the merged watched-wallet list from Octopus files (any JSON; every 0x+40-hex address in it)
compare  join two boxes' CSVs (e.g. Tokyo vs Dublin) by (feed, tx, wallet); also alch vs rtds on one box
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from orjson import loads
except ImportError:
    from json import loads


EXCHANGES = ["0xE111180000d2663C0091e4f400237545B87B996B", "0xe3333700ca9d93003f00f0f71f8515005f6c00aa"]  # octopus config.py
FILL_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
RTDS = "wss://ws-live-data.polymarket.com"
ADDR = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
COLS = ["feed", "local_us", "tx", "wallet", "role", "side", "asset", "price", "size", "slug", "src_ts", "block"]


def load_wallets(files):
    out = set()
    for f in files:
        try:
            out |= {a.lower() for a in ADDR.findall(Path(f).read_text())}
        except OSError:
            pass
    out -= {a.lower() for a in EXCHANGES}
    out.discard("0x" + "0" * 40)
    return out


def parse_log(lg, watched):
    """Alchemy log -> row dict (or None). owner = topics[2], counterparty = topics[3]."""
    t = lg.get("topics") or []
    if len(t) < 4 or lg.get("removed"):
        return None
    owner, cp = "0x" + t[2][-40:].lower(), "0x" + t[3][-40:].lower()
    wallet, role = (owner, "owner") if owner in watched else (cp, "counterparty") if cp in watched else (None, None)
    if wallet is None:
        return None
    d = (lg.get("data") or "0x")[2:]
    side = asset = ""
    if len(d) >= 128:
        side = "BUY" if int(d[0:64], 16) == 0 else "SELL"
        asset = str(int(d[64:128], 16))
    return dict(feed="alch", tx=lg.get("transactionHash", ""), wallet=wallet, role=role, side=side, asset=asset,
                price="", size="", slug="", src_ts="", block=int(lg.get("blockNumber", "0x0"), 16))  # fmt: skip


def parse_rtds(msg, watched):
    """RTDS activity/trades message -> (row or None, message ts ms or None)."""
    p = msg.get("payload") or {}
    ts = msg.get("timestamp")
    w = str(p.get("proxyWallet") or p.get("proxy_wallet") or "").lower()
    if msg.get("topic") != "activity" or w not in watched:
        return None, ts
    return dict(feed="rtds", tx=p.get("transactionHash") or p.get("transaction_hash") or "", wallet=w, role="trader",
                side=p.get("side", ""), asset=str(p.get("asset", "")), price=p.get("price", ""), size=p.get("size", ""),
                slug=p.get("slug") or p.get("eventSlug") or "", src_ts=p.get("timestamp", ts), block=""), ts  # fmt: skip


class Out:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write(self, row):
        row["local_us"] = row.get("local_us") or time.time_ns() // 1000
        f = self.root / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".csv")
        new = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in COLS})


async def alch(st, out):
    import websockets

    key = os.environ.get("ALCHEMY_API_KEY", "")
    if not key:
        st["alch_err"] = "no ALCHEMY_API_KEY"
        return
    url = f"wss://polygon-mainnet.g.alchemy.com/v2/{key}"
    while True:
        watched = st["watched"]
        gen = st["gen"]
        pad = ["0x" + "0" * 24 + a[2:] for a in sorted(watched)]
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                for i, slot in ((1, [FILL_TOPIC, None, pad]), (2, [FILL_TOPIC, None, None, pad])):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "eth_subscribe",
                                              "params": ["logs", {"address": EXCHANGES, "topics": slot}]}))  # fmt: skip
                st["alch_conn"] = 1
                while st["gen"] == gen:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        continue
                    lus = time.time_ns() // 1000
                    m = loads(raw)
                    lg = (m.get("params") or {}).get("result")
                    if isinstance(lg, dict):
                        r = parse_log(lg, watched)
                        if r:
                            r["local_us"] = lus
                            out.write(r)
                            st["alch_rows"] += 1
        except Exception as e:  # noqa: BLE001
            st["alch_err"] = repr(e)[:160].replace(key, "***")
        st["alch_conn"] = 0
        await asyncio.sleep(2)


async def rtds(st, out):
    import websockets

    while True:
        try:
            async with websockets.connect(RTDS, ping_interval=10, max_size=None) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}))
                st["rtds_conn"] = 1
                last = time.time()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        raw = None
                    now = time.time()
                    if not raw:
                        if now - last > 30:  # RTDS can stall with the socket open
                            st["rtds_stalls"] += 1
                            break
                        continue
                    last = now
                    lus = time.time_ns() // 1000
                    try:
                        m = loads(raw)
                    except ValueError:
                        continue
                    for msg in m if isinstance(m, list) else (m,):
                        if not isinstance(msg, dict):
                            continue
                        r, ts = parse_rtds(msg, st["watched"])
                        st["rtds_msgs"] += 1
                        if isinstance(ts, (int, float)) and ts > 1e12:
                            st["rtds_lag"].append(lus / 1000 - ts)
                        if r:
                            r["local_us"] = lus
                            out.write(r)
                            st["rtds_rows"] += 1
        except Exception as e:  # noqa: BLE001
            st["rtds_err"] = repr(e)[:160]
        st["rtds_conn"] = 0
        await asyncio.sleep(2)


async def housekeeping(st, files):
    while True:
        await asyncio.sleep(60)
        w = load_wallets(files)
        if w and w != st["watched"]:
            st["watched"] = w
            st["gen"] += 1  # alch resubscribes
        lag = st["rtds_lag"]
        q = [round(float(np.percentile(lag, p))) for p in (50, 90, 99)] if lag else []
        print(f"wallets={len(st['watched'])} alch conn={st['alch_conn']} rows={st['alch_rows']} err={st['alch_err']} | "
              f"rtds conn={st['rtds_conn']} msgs/min={st['rtds_msgs']} rows={st['rtds_rows']} lag_ms(p50/90/99)={q} "
              f"stalls={st['rtds_stalls']} err={st['rtds_err']}", flush=True)  # fmt: skip
        st["rtds_msgs"], st["rtds_lag"] = 0, []


def cmd_run(a):
    w = load_wallets(a.wallets)
    if not w:
        sys.exit(f"no wallet addresses found in {a.wallets}")
    st = dict(watched=w, gen=0, alch_conn=0, alch_rows=0, alch_err=None, rtds_conn=0, rtds_msgs=0, rtds_rows=0,
              rtds_lag=[], rtds_stalls=0, rtds_err=None)  # fmt: skip
    print(f"watching {len(w)} wallets", flush=True)
    out = Out(a.out)

    async def main():
        await asyncio.gather(alch(st, out), rtds(st, out), housekeeping(st, a.wallets))

    asyncio.run(main())


def cmd_wallets(a):
    print(json.dumps(sorted(load_wallets(a.wallets)), indent=0))


def first_seen(d, since=""):
    import pandas as pd

    fs = [f for f in sorted(Path(d).glob("*.csv")) if f.stem >= since]
    if not fs:
        return pd.DataFrame(columns=["feed", "tx", "wallet", "local_us"])
    D = pd.concat([pd.read_csv(f, dtype=str) for f in fs], ignore_index=True)
    D = D[D.tx.notna() & (D.tx != "")]
    D["local_us"] = D.local_us.astype("int64")
    D["tx"] = D.tx.str.lower()
    return D.groupby(["feed", "tx", "wallet"], as_index=False).local_us.min()


def q(v):
    v = np.asarray(v, float)
    return f"n={len(v):>5} p10 {np.percentile(v, 10):>7.0f} p50 {np.percentile(v, 50):>7.0f} p90 {np.percentile(v, 90):>7.0f} ms" if len(v) else "n=0"


def cmd_compare(a):
    A, B = first_seen(a.a, a.since), first_seen(a.b, a.since)
    print(f"A={a.a}  B={a.b}  (B - A, ms; negative = B saw it first)")
    for feed in ("alch", "rtds"):
        J = A[A.feed == feed].merge(B[B.feed == feed], on=["tx", "wallet"], suffixes=("_a", "_b"))
        print(f"  {feed}: {q((J.local_us_b - J.local_us_a) / 1000)}")
    for name, D in (("A", A), ("B", B)):
        J = D[D.feed == "alch"].merge(D[D.feed == "rtds"], on=["tx", "wallet"], suffixes=("_alch", "_rtds"))
        print(f"  box {name}: alch - rtds {q((J.local_us_alch - J.local_us_rtds) / 1000)}  (positive = RTDS earlier)")
        for feed in ("alch", "rtds"):
            print(f"  box {name}: {feed} fills seen {int((D.feed == feed).sum())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("run")
    p.add_argument("--wallets", nargs="+", required=True, help="Octopus wallet files (any JSON with addresses)")
    p.add_argument("--out", type=Path, default=Path.home() / "sig-shadow")
    p = sp.add_parser("wallets")
    p.add_argument("--wallets", nargs="+", required=True)
    p = sp.add_parser("compare")
    p.add_argument("--a", type=Path, required=True)
    p.add_argument("--b", type=Path, required=True)
    p.add_argument("--since", default="")
    a = ap.parse_args()
    {"run": cmd_run, "wallets": cmd_wallets, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
