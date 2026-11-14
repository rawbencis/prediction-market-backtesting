"""Taker census for the recorded up/down markets.

Polymarket's activity stream (RTDS activity/trades, public, no key) carries the wallet behind every trade.
This keeps the trades on the shadow recorder's markets (btc/eth/sol/xrp/doge up/down, 5m and 15m) and writes
them next to the books: ~/shadow-rec/YYYY-MM-DD/act-HH.bin, one ACT_REC per trade.

  python3 act_rec.py rec   --out ~/shadow-rec          (systemd: updown-act)
  python3 act_rec.py top   --rec ~/shadow-rec [--days D ...] [--asset btc] [--tf 5]
                                                        wallets ranked by volume, with OhioAI marked
"""

import argparse
import asyncio
import collections
import json
import re
import struct
import time
from pathlib import Path

try:
    from orjson import loads
except ImportError:  # pragma: no cover
    from json import loads

ASSETS = ("btc", "eth", "sol", "xrp", "doge")
URL = "wss://ws-live-data.polymarket.com"
SUB = json.dumps({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]})
SLUG = re.compile(r"^(btc|eth|sol|xrp|doge)-updown-(5|15)m-(\d+)$")
OHIOAI = "0x129246beaac356e104c397f0985c11b7469be5a6"
# local_us, server emit ms, trade ts s, asset, tf, epoch, tok (0 up / 1 down), side (0 buy / 1 sell),
# price, size, fee (raw payload field), wallet (20 bytes), tx hash (32 bytes)
ACT_REC = struct.Struct("<qqIBBIBBfff20s32s")
ACT_DT = [("l", "<i8"), ("x", "<i8"), ("ts", "<u4"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("tok", "u1"),
          ("sd", "u1"), ("p", "<f4"), ("sz", "<f4"), ("fee", "<f4"), ("w", "S20"), ("tx", "S32")]  # fmt: skip


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _hex(h, n):
    h = str(h or "")
    if not h.strip("0x"):
        return None
    try:
        return bytes.fromhex(h[2:] if h.startswith("0x") else h)[:n].ljust(n, b"\0")
    except ValueError:
        return None


def pack(lus, env_ms, p):
    """One RTDS trade payload -> ACT_REC bytes, or None if it is not on a recorded market or malformed."""
    m = SLUG.match(str(p.get("slug") or ""))
    if not m:
        return None
    out = str(p.get("outcome") or "").lower()
    tok = 0 if out == "up" else 1 if out == "down" else None
    side = {"BUY": 0, "SELL": 1}.get(str(p.get("side") or "").upper())
    price, size = _f(p.get("price")), _f(p.get("size"))
    w, tx = _hex(p.get("proxyWallet"), 20), _hex(p.get("transactionHash"), 32)
    if tok is None or side is None or price is None or size is None or w is None or tx is None:
        return None
    ts = int(_f(p.get("timestamp")) or 0)
    ts = ts // 1000 if ts > 10**11 else ts
    fee = _f(p.get("fee"))
    return ACT_REC.pack(lus, int(env_ms or 0), ts, ASSETS.index(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tok, side, price, size, -1.0 if fee is None else fee, w, tx)  # fmt: skip


def frames(raw):
    """Yield (envelope ms, payload) for each activity trade in one websocket frame."""
    if not raw or raw[0] not in "{[":
        return
    try:
        msg = loads(raw)
    except ValueError:
        return
    for it in msg if isinstance(msg, list) else (msg,):
        if isinstance(it, dict) and it.get("topic") == "activity" and it.get("type") in ("trades", "trade"):
            pl = it.get("payload")
            for p in pl if isinstance(pl, list) else (pl,):
                if isinstance(p, dict):
                    yield it.get("timestamp"), p


class Out:
    def __init__(self, root):
        self.root, self.buf, self.n = root, [], 0

    def flush(self):
        if not self.buf:
            return
        t = time.gmtime()
        d = self.root / time.strftime("%Y-%m-%d", t)
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"act-{t.tm_hour:02d}.bin").open("ab") as f:
            f.write(b"".join(self.buf))
        self.n += len(self.buf)
        self.buf.clear()


async def stream(out, st):
    import websockets

    while True:
        try:
            async with websockets.connect(URL, open_timeout=10, ping_interval=None, max_size=None) as ws:
                await ws.send(SUB)
                st["conn"] += 1
                last = time.time()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        if time.time() - last > 20:  # every Polymarket trade flows here: 20 s of silence is a dead socket
                            st["stalls"] += 1
                            break
                        await ws.send("ping")
                        continue
                    lus = time.time_ns() // 1000
                    last = lus / 1e6
                    st["msgs"] += 1
                    for env, p in frames(raw if isinstance(raw, str) else raw.decode("utf-8", "replace")):
                        st["trades"] += 1
                        if env:
                            st["lag"].append(lus / 1000 - float(env))
                        row = pack(lus, env, p)
                        if row is not None:
                            out.buf.append(row)
        except Exception as e:  # noqa: BLE001
            st["err"] = repr(e)[:160]
        await asyncio.sleep(1)


async def housekeeping(out, st):
    k = 0
    while True:
        await asyncio.sleep(1)
        out.flush()
        k += 1
        if k % 60 == 0:
            lag = sorted(st["lag"])
            pl = [round(lag[int(len(lag) * q)]) for q in (0.1, 0.5, 0.9)] if lag else None
            print(f"act rows={out.n} msgs={st['msgs']} trades={st['trades']} lag_ms={pl} conns={st['conn']} "
                  f"stalls={st['stalls']} last_err={st['err']}", flush=True)  # fmt: skip
            st["lag"].clear()
            st["err"] = None


async def run(a):
    a.out.mkdir(parents=True, exist_ok=True)
    out = Out(a.out)
    st = {"msgs": 0, "trades": 0, "lag": [], "conn": 0, "stalls": 0, "err": None}
    await asyncio.gather(stream(out, st), housekeeping(out, st))


def read(root, days):
    import numpy as np

    parts = []
    for d in days:
        for f in sorted((root / d).glob("act-*.bin")):
            b = f.read_bytes()
            b = b[: len(b) - len(b) % ACT_REC.size]
            parts.append(np.frombuffer(b, dtype=np.dtype(ACT_DT)))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.dtype(ACT_DT))


def cmd_top(a):
    import numpy as np

    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if any(p.glob("act-*.bin")))[-1:]
    r = read(a.rec, days)
    if a.asset:
        r = r[r["a"] == ASSETS.index(a.asset)]
    if a.tf:
        r = r[r["tf"] == a.tf]
    if not len(r):
        print("no rows")
        return
    usd = r["p"].astype(float) * r["sz"]
    per_tx = collections.Counter(r["tx"].tolist())
    rows_per_tx = collections.Counter(per_tx.values())
    print(f"{days} rows={len(r)} wallets={len(set(r['w'].tolist()))} txs={len(per_tx)} "
          f"rows/tx={dict(sorted(rows_per_tx.items())[:6])} fee>0 share={float((r['fee'] > 0).mean()):.3f} "
          f"fee<0(missing)={float((r['fee'] < 0).mean()):.3f}")  # fmt: skip
    agg = collections.defaultdict(lambda: [0, 0.0, 0, 0.0])
    for w, u, sd, fee in zip(r["w"].tolist(), usd, r["sd"].tolist(), r["fee"].tolist()):
        g = agg[w]
        g[0] += 1
        g[1] += u
        g[2] += sd == 0
        g[3] += fee > 0
    tot = sum(g[1] for g in agg.values())
    print(f"{'wallet':44} {'trades':>7} {'usd':>10} {'share':>6} {'buy%':>5} {'fee>0%':>6}")
    for w, g in sorted(agg.items(), key=lambda kv: -kv[1][1])[: a.n]:
        addr = "0x" + w.hex()
        tag = "  <- OhioAI" if addr == OHIOAI else ""
        print(f"{addr:44} {g[0]:7d} {g[1]:10.0f} {g[1] / tot:6.1%} {g[2] / g[0]:5.0%} {g[3] / g[0]:6.0%}{tag}")
    oh = agg.get(bytes.fromhex(OHIOAI[2:]))
    if oh:
        rank = 1 + sum(1 for g in agg.values() if g[1] > oh[1])
        print(f"OhioAI rank by volume: {rank} of {len(agg)}")
    _ = np


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("rec")
    p.add_argument("--out", type=Path, default=Path.home() / "shadow-rec")
    p = sp.add_parser("top")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--days", nargs="*")
    p.add_argument("--asset", choices=ASSETS)
    p.add_argument("--tf", type=int, choices=(5, 15))
    p.add_argument("--n", type=int, default=25)
    a = ap.parse_args()
    if a.cmd == "rec":
        asyncio.run(run(a))
    else:
        cmd_top(a)


if __name__ == "__main__":
    main()
