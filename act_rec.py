"""Taker census for the recorded up/down markets.

Polymarket's activity stream (RTDS activity/trades, public, no key) carries the wallet behind every trade.
This keeps the trades on the shadow recorder's markets (btc/eth/sol/xrp/doge up/down, 5m and 15m) and writes
them next to the books: ~/shadow-rec/YYYY-MM-DD/act-HH.bin, one ACT_REC per trade.

  python3 act_rec.py rec   --out ~/shadow-rec          (systemd: updown-act)
  python3 act_rec.py top   --rec ~/shadow-rec [--days D ...] [--asset btc] [--tf 5]
                                                        wallets ranked by volume, with OhioAI marked
  python3 act_rec.py pnl   --rec ~/shadow-rec [--days D ...] [--asset btc] [--tf 5] [--min-usd 2000]
                                                        wallets ranked by settled PnL on these markets
  python3 act_rec.py profile --wallets 0x523c 0x13c6 ... [--asset btc] [--tf 5]
                                                        where in the window and at what price each wallet (and all
                                                        taker flow) makes or loses money
  python3 act_rec.py fills --wallet 0x523c [--asset btc] [--tf 5]
                                                        every fill with exchange time, time left, Binance move since
                                                        the window opened, book mid, outcome and counterparties

The stream emits one row per participant of a fill (taker + each maker; 2+ rows per tx), and only the taker's row
carries a fee, so fee > 0 marks the taker side.
"""

import argparse
import asyncio
import calendar
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


def settled_pnl(r, winners, fee_rate=0.07):
    """Per-row settled PnL in $ on these markets: buy (win - p)*size, sell (p - win)*size, minus the taker fee.
    Exact for anything held to settlement or round-tripped; a minted pair sold both ways nets p_up + p_down - 1.
    Rows in markets without a winner get NaN."""
    import numpy as np

    key = [f"{ASSETS[a]}-updown-{tf}m-{e}" for a, tf, e in zip(r["a"].tolist(), r["tf"].tolist(), r["e"].tolist())]
    won = np.array([{"UP": 0, "DOWN": 1}.get(winners.get(k), -1) for k in key])
    win = np.where(won < 0, np.nan, (won == r["tok"]).astype(float))
    p, sz = r["p"].astype(float), r["sz"].astype(float)
    taker = r["fee"] > 0
    pnl = np.where(r["sd"] == 0, win - p, p - win) * sz - taker * fee_rate * p * (1 - p) * sz
    return pnl, np.array(key)


def cmd_pnl(a):
    import numpy as np

    import shadow_eval as se

    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if any(p.glob("act-*.bin")))
    r = read(a.rec, days)
    if a.asset:
        r = r[r["a"] == ASSETS.index(a.asset)]
    if a.tf:
        r = r[r["tf"] == a.tf]
    keys = sorted({f"{ASSETS[x]}-updown-{t}m-{e}" for x, t, e in set(zip(r["a"].tolist(), r["tf"].tolist(), r["e"].tolist()))})
    winners = se.settle(a.settle, keys, not a.no_fetch)
    pnl, key = settled_pnl(r, winners)
    ok = ~np.isnan(pnl)
    r, pnl, key = r[ok], pnl[ok], key[ok]
    usd = r["p"].astype(float) * r["sz"]
    print(f"days {days[0]}..{days[-1]} ({len(days)}): {int(ok.sum())} rows in {len(set(key))} settled markets "
          f"({len(keys) - len(set(key))} unsettled skipped)")  # fmt: skip
    rng = np.random.default_rng(7)
    rows, few = [], 0
    order = np.argsort(r["w"], kind="stable")
    r, pnl, key, usd = r[order], pnl[order], key[order], usd[order]
    uw, start = np.unique(r["w"], return_index=True)
    bounds = list(start) + [len(r)]
    for i, w in enumerate(uw):
        m = slice(bounds[i], bounds[i + 1])
        u = float(usd[m].sum())
        if u < a.min_usd:
            continue
        pm = pnl[m]
        g = {}
        for k, x, y in zip(key[m].tolist(), pm.tolist(), usd[m].tolist()):
            s = g.setdefault(k, [0.0, 0.0])
            s[0] += x
            s[1] += y
        arr = np.array(list(g.values()))
        if len(arr) < a.min_markets:
            few += 1
            continue
        idx = rng.integers(0, len(arr), (500, len(arr)))
        boot = 100 * arr[idx, 0].sum(1) / np.maximum(arr[idx, 1].sum(1), 1e-9)
        tk = r["fee"][m] > 0
        rows.append(("0x" + w.hex(), len(pm), u, float(pm.sum()), 100 * float(pm.sum()) / u,
                     *np.percentile(boot, [5, 95]), float(tk.mean()), len(arr),
                     100 * float(pm[tk].sum() / max(usd[m][tk].sum(), 1e-9)) if tk.any() else float("nan"),
                     100 * float(pm[~tk].sum() / max(usd[m][~tk].sum(), 1e-9)) if (~tk).any() else float("nan")))  # fmt: skip
    rows.sort(key=lambda x: -x[3])
    hdr = f"{'wallet':44} {'fills':>6} {'usd':>9} {'pnl $':>8} {'c/$':>6} {'90% CI (by market)':>17} {'taker':>5} {'mkts':>5} {'tk c/$':>7} {'mk c/$':>7}"
    print(f"{len(rows)} wallets with >= ${a.min_usd:,.0f} traded in >= {a.min_markets} markets "
          f"({few} more traded in fewer markets: a bootstrap over a handful of markets can't separate skill from a "
          f"streak, so they are left out)")  # fmt: skip

    def show(x):
        tag = "  <- OhioAI" if x[0] == OHIOAI else ""
        print(f"{x[0]:44} {x[1]:6d} {x[2]:9.0f} {x[3]:+8.0f} {x[4]:+6.2f} [{x[5]:+6.2f},{x[6]:+6.2f}] {x[7]:5.0%} {x[8]:5d} "
              f"{x[9]:+7.2f} {x[10]:+7.2f}{tag}")  # fmt: skip

    print("TOP by settled PnL\n" + hdr)
    for x in rows[: a.n]:
        show(x)
    print("BOTTOM\n" + hdr)
    for x in rows[-min(a.n, 10) :]:
        show(x)
    oh = [x for x in rows if x[0] == OHIOAI]
    if oh:
        print(f"OhioAI rank by PnL: {rows.index(oh[0]) + 1} of {len(rows)}")
        show(oh[0])
    tot = sum(x[3] for x in rows)
    print(f"sum of PnL across listed wallets: {tot:+.0f} $ (fees leave the system, so the total is negative)")


T_BUCKETS = ((180, 1e9, "300-180s"), (120, 180, "180-120s"), (60, 120, "120-60s"), (30, 60, "60-30s"),
             (10, 30, "30-10s"), (-1e9, 10, "<10s"))  # fmt: skip
P_BUCKETS = ((0, 0.2, "<.20"), (0.2, 0.4, ".20-.40"), (0.4, 0.6, ".40-.60"), (0.6, 0.8, ".60-.80"),
             (0.8, 0.9, ".80-.90"), (0.9, 0.95, ".90-.95"), (0.95, 1.01, ">=.95"))  # fmt: skip


def _breakdown(r, pnl, key, title, out=print):
    import numpy as np

    usd = r["p"].astype(float) * r["sz"]
    t_left = r["e"].astype(float) + 60.0 * r["tf"] - r["ts"].astype(float)
    tk = r["fee"] > 0
    out(f"  {title}: {len(r)} fills, {len(set(key.tolist()))} markets, ${usd.sum():,.0f}, "
        f"pnl {pnl.sum():+,.0f} $ ({100 * pnl.sum() / max(usd.sum(), 1e-9):+.2f} c/$), taker {tk.mean():.0%}, "
        f"buys {(r['sd'] == 0).mean():.0%}, median size {np.median(r['sz']):.0f} sh")  # fmt: skip
    for lab, col, buckets in (("time left", t_left, T_BUCKETS), ("price", r["p"].astype(float), P_BUCKETS)):
        cells = []
        for lo, hi, name in buckets:
            m = (col >= lo) & (col < hi)
            if m.sum():
                cells.append(f"{name} n={int(m.sum())} {100 * pnl[m].sum() / max(usd[m].sum(), 1e-9):+.1f}c/$"
                             f" ${usd[m].sum() / 1000:.1f}k")  # fmt: skip
        out(f"    by {lab:9}: " + " | ".join(cells))


def cmd_profile(a):
    import numpy as np

    import shadow_eval as se

    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if any(p.glob("act-*.bin")))
    r = read(a.rec, days)
    if a.asset:
        r = r[r["a"] == ASSETS.index(a.asset)]
    if a.tf:
        r = r[r["tf"] == a.tf]
    keys = sorted({f"{ASSETS[x]}-updown-{t}m-{e}" for x, t, e in set(zip(r["a"].tolist(), r["tf"].tolist(), r["e"].tolist()))})
    pnl, key = settled_pnl(r, se.settle(a.settle, keys, not a.no_fetch))
    ok = ~np.isnan(pnl)
    r, pnl, key = r[ok], pnl[ok], key[ok]
    print(f"days {days[0]}..{days[-1]}, {len(r)} settled rows")
    tk = r["fee"] > 0
    _breakdown(r[tk], pnl[tk], key[tk], "ALL TAKER FLOW")
    _breakdown(r[~tk], pnl[~tk], key[~tk], "ALL MAKER FLOW")
    hexw = np.array(["0x" + w.hex() for w in r["w"].tolist()])
    for pre in a.wallets:
        m = np.char.startswith(hexw, pre.lower())
        ws = sorted(set(hexw[m].tolist()))
        if not ws:
            print(f"\n{pre}: no rows")
            continue
        for w in ws[:3]:
            mm = hexw == w
            print(f"\n{w}" + ("  <- OhioAI" if w == OHIOAI else ""))
            _breakdown(r[mm], pnl[mm], key[mm], "all")


def _hours(ms_list, back=1):
    """{(day, hour)} covering each exchange time and the `back` hours before it (UTC file names)."""
    out = set()
    for ms in ms_list:
        for k in range(back + 1):
            t = time.gmtime(ms / 1000 - 3600 * k)
            out.add((time.strftime("%Y-%m-%d", t), t.tm_hour))
    return out


def _thin(x, ms=100):
    """Last row per (asset, ms bucket) by exchange time: enough for 1 s / 5 s moves, a fraction of the memory."""
    import numpy as np

    if not len(x):
        return x
    o = np.lexsort((x["T"], x["a"]))
    x = x[o]
    key = x["a"].astype(np.int64) * 10**13 + x["T"] // ms
    last = np.r_[key[1:] != key[:-1], True]
    return x[last]


def _load(root, kind, dt, hours, keep):
    """Read only the given hour files, filtering each as it is read (keeps memory flat on the recorder box)."""
    import numpy as np

    parts = []
    for d, h in sorted(hours):
        f = root / d / f"{kind}-{h:02d}.bin"
        if f.exists():
            x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
            x = x[keep(x)]
            parts.append(_thin(x) if kind == "bn" else x)
    return np.concatenate(parts) if parts else np.zeros(0, dt)


def cmd_fills(a):
    import numpy as np

    import shadow_eval as se
    from ohioai_match import TR_DT

    dt = np.dtype(ACT_DT)
    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if any(p.glob("act-*.bin")))
    files = [f for d in days for f in sorted((a.rec / d).glob("act-*.bin"))]

    def scoped(x):
        m = np.ones(len(x), bool)
        if a.asset:
            m &= x["a"] == ASSETS.index(a.asset)
        if a.tf:
            m &= x["tf"] == a.tf
        return m

    mine, who = [], None
    for f in files:  # pass 1: the wallet's rows (match the prefix on each file's distinct wallets only)
        x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
        x = x[scoped(x)]
        hit = [w for w in np.unique(x["w"]).tolist() if ("0x" + w.ljust(20, b"\0").hex()).startswith(a.wallet.lower())]
        if hit:
            who = who or "0x" + hit[0].ljust(20, b"\0").hex()
            mine.append(x[x["w"] == hit[0] if len(hit) == 1 else np.isin(x["w"], hit)])
    if not mine:
        print("no rows")
        return
    me = np.concatenate(mine)
    txl = np.unique(me["tx"])
    others = {}
    for f in files:  # pass 2: everyone else in the same fills
        x = np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize)
        x = x[np.isin(x["tx"], txl)]
        for t, w in zip(x["tx"].tolist(), x["w"].tolist()):
            others.setdefault(t, set()).add("0x" + w.ljust(20, b"\0").hex())
    keys = sorted({f"{ASSETS[x]}-updown-{t}m-{e}" for x, t, e in set(zip(me["a"].tolist(), me["tf"].tolist(), me["e"].tolist()))})
    win = se.settle(a.settle, keys, not a.no_fetch)
    ts_ms = [int(t) * 1000 for t in me["ts"].tolist()]
    tr = _load(a.rec, "tr", TR_DT, _hours(ts_ms) | _hours([t + 3_600_000 for t in ts_ms], 0),
               lambda x: np.isin(x["tx"], txl))  # fmt: skip
    xms = {}
    for t, xx in zip(tr["tx"].tolist(), tr["x"].tolist()):
        xms.setdefault(t, int(xx))
    fill_ms = [xms.get(t, int(xx) or int(s) * 1000) for t, xx, s in zip(me["tx"].tolist(), me["x"].tolist(), me["ts"].tolist())]
    opens = [int(e) * 1000 for e in me["e"].tolist()]
    assets = np.unique(me["a"])
    if getattr(a, "no_binance", False):  # Polymarket-only analysis: no Binance rows read, move columns NaN
        bn = np.zeros(0, se.BN_DT)
    else:
        bn = _load(a.rec, "bn", se.BN_DT, _hours(fill_ms + opens), lambda x: (x["src"] == 0) & np.isin(x["a"], assets))
    # Book mid just before each match, streamed one fill-hour at a time (that hour's and the previous hour's file,
    # filtered to the windows filled in that hour): a wallet active in hundreds of markets never holds more than two
    # filtered hours of book rows. Loading all of them at once was OOM-killed at the 1.2 GB cap (2026-11-16).
    mids = np.full(len(me), np.nan)
    feat = [None] * len(me)  # Polymarket-only state just before each match (see _book_features)
    by_hour = {}
    for k, x in enumerate(fill_ms):
        by_hour.setdefault(next(iter(_hours([x], 0))), []).append(k)
    for hk, ks in by_hour.items():
        eps = np.unique(me["e"][ks])
        prev = time.gmtime(calendar.timegm(time.strptime(f"{hk[0]} {hk[1]:02d}", "%Y-%m-%d %H")) - 3600)
        hours = {hk, (time.strftime("%Y-%m-%d", prev), prev.tm_hour)}
        tfs = np.unique(me["tf"][ks])
        # every asset's book for those windows: the other assets' books are the Polymarket-only lead signal
        pm = _load(a.rec, "pm", se.PM_DT, hours, lambda x, eps=eps, tfs=tfs: np.isin(x["e"], eps) & np.isin(x["tf"], tfs))
        pm = pm[np.lexsort((pm["x"], pm["e"], pm["tf"], pm["a"]))]
        gk = (pm["a"].astype(np.int64) << 40) | (pm["tf"].astype(np.int64) << 32) | pm["e"]
        cuts = np.r_[0, np.flatnonzero(np.diff(gk)) + 1, len(pm)]
        books = {(int(pm["a"][c0]), int(pm["tf"][c0]), int(pm["e"][c0])): pm[c0:c1] for c0, c1 in zip(cuts[:-1], cuts[1:]) if c1 > c0}
        for k in ks:
            row, x = me[k], fill_ms[k]
            pp = books.get((int(row["a"]), int(row["tf"]), int(row["e"])))
            i = np.searchsorted(pp["x"], x - 1, "right") - 1 if pp is not None else -1  # book standing just before the match
            mid = (int(pp["b"][i]) + int(pp["k"][i])) / 2e4 if i >= 0 else float("nan")
            mids[k] = mid if row["tok"] == 0 else 1 - mid
            others_ = [v for (aa, tf, e), v in books.items() if aa != row["a"] and tf == row["tf"] and e == row["e"]]
            feat[k] = _book_features(pp, i, x, int(row["tok"]), float(row["p"]), others_) if i >= 0 else {}
        del pm, books
    print(f"{who}: {len(me)} rows, {sum(t in xms for t in txl.tolist())} of {len(txl)} txs found in trade prints")
    pnl, _ = settled_pnl(me, win)
    rows = []
    for k in np.lexsort((me["ts"], me["e"])):
        row, x = me[k], fill_ms[k]
        e0, end = int(row["e"]) * 1000, (int(row["e"]) + 60 * int(row["tf"])) * 1000
        bb = bn[bn["a"] == row["a"]]
        sgn = 1 if row["tok"] == 0 else -1

        def px(tms, bb=bb):
            i = np.searchsorted(bb["T"], tms, "right") - 1
            return float(bb["p"][i]) if i >= 0 else float("nan")

        p_now = px(x)
        mid = float(mids[k])
        key = f"{ASSETS[row['a']]}-updown-{row['tf']}m-{row['e']}"
        rows.append(dict(
            x=x, left=(end - x) / 1000, tok="UP" if row["tok"] == 0 else "DOWN", side="BUY" if row["sd"] == 0 else "SELL",
            role="taker" if row["fee"] > 0 else "maker", p=float(row["p"]), sz=float(row["sz"]),
            mv_open=sgn * 1e4 * np.log(p_now / px(e0)), mv_1s=sgn * 1e4 * np.log(p_now / px(x - 1000)),
            mv_5s=sgn * 1e4 * np.log(p_now / px(x - 5000)), mid=mid, vs_mid=float(row["p"]) - mid,
            out="?" if win.get(key) is None else "W" if win.get(key) == ("UP" if row["tok"] == 0 else "DOWN") else "L",
            pnl=float(pnl[k]), usd=float(row["p"]) * float(row["sz"]), key=key, **(feat[k] or {}),
            cps=sorted(others.get(row["tx"], set()) - {who})))  # fmt: skip
    if a.summary:
        _fills_summary(rows, binance=not getattr(a, "no_binance", False))
        return
    print(f"{'exchange time UTC':23} {'left s':>6} {'tok':4} {'side':4} {'role':5} {'price':>5} {'size':>7} "
          f"{'bn open':>8} {'bn 1s':>6} {'bn 5s':>6} {'mid':>6} {'out':3}  counterparties")  # fmt: skip
    for r in rows:
        x = r["x"]
        print(f"{time.strftime('%m-%d %H:%M:%S', time.gmtime(x / 1000))}.{x % 1000:03d} {r['left']:6.1f} {r['tok']:4} "
              f"{r['side']:4} {r['role']:5} {r['p']:5.2f} {r['sz']:7.0f} {r['mv_open']:+7.1f} {r['mv_1s']:+6.1f} "
              f"{r['mv_5s']:+6.1f} {r['mid']:6.3f} {r['out']:3}  {len(r['cps'])}: " + " ".join(c[:8] for c in r["cps"][:4]))  # fmt: skip


def _book_features(pp, i, x, tok, p, others, ms=500):
    """State of Polymarket's own books just before a fill at exchange time x, seen from the token bought.
    pp: this market's UP top-of-book rows sorted by x; i: last row before the match; others: the other assets' rows.
      tick     fill price minus the token's best bid, in ticks (0 = at the best bid, -1 = one behind, +1 = improved)
      spr      spread in ticks
      imb      (size at the token's best bid - size at its best ask) / sum; the wallet's own order is part of the bid
      calm     ms since the best bid or ask last changed
      own1s    this book's mid move over the last 1 s toward the token, cents
      alt      the largest other-asset UP-mid move over the last `ms` ms, signed toward the token, cents"""
    import numpy as np

    b, k = int(pp["b"][i]), int(pp["k"][i])
    if b <= 0 or k <= b:
        return {}
    sgn = 1 if tok == 0 else -1
    bid, bsz, asz = (b / 1e4, float(pp["bs"][i]), float(pp["ks"][i])) if tok == 0 else (1 - k / 1e4, float(pp["ks"][i]), float(pp["bs"][i]))
    chg = np.flatnonzero((pp["b"][1 : i + 1] != pp["b"][:i]) | (pp["k"][1 : i + 1] != pp["k"][:i]))
    f = dict(tick=round((p - bid) * 100), spr=round((k - b) / 100), imb=(bsz - asz) / (bsz + asz) if bsz + asz > 0 else float("nan"), lvl=bsz,
             calm=float(x - int(pp["x"][chg[-1] + 1])) if len(chg) else float(x - int(pp["x"][0])))  # fmt: skip

    def mid_at(q, t):
        j = np.searchsorted(q["x"], t, "right") - 1
        return (int(q["b"][j]) + int(q["k"][j])) / 2e4 if j >= 0 and q["b"][j] > 0 and q["k"][j] > q["b"][j] else float("nan")

    f["own1s"] = 100 * sgn * (mid_at(pp, x - 1) - mid_at(pp, x - 1000))
    mv = [mid_at(q, x - 1) - mid_at(q, x - ms) for q in others]
    mv = [m for m in mv if m == m]
    f["alt"] = 100 * sgn * max(mv, key=abs) if mv else float("nan")
    return f


def _pairs_summary(rows, out=print):
    """Per market: shares and average price bought on each side; how much of the book is paired and at what cost."""
    by = {}
    for r in rows:
        if r["side"] != "BUY":
            continue
        d = by.setdefault(r["key"], {"UP": [0.0, 0.0], "DOWN": [0.0, 0.0], "pnl": 0.0})
        d[r["tok"]][0] += r["sz"]
        d[r["tok"]][1] += r["usd"]
        d["pnl"] += r["pnl"]
    both = [d for d in by.values() if d["UP"][0] > 0 and d["DOWN"][0] > 0]
    paired = sum(min(d["UP"][0], d["DOWN"][0]) for d in both)
    cost = sum(min(d["UP"][0], d["DOWN"][0]) * (d["UP"][1] / d["UP"][0] + d["DOWN"][1] / d["DOWN"][0]) for d in both)
    shares = sum(d["UP"][0] + d["DOWN"][0] for d in by.values())
    locked = paired - cost
    total = sum(d["pnl"] for d in by.values())
    out(f"  pairs: {len(by)} markets, both sides bought in {len(both)}; {2 * paired:,.0f} of {shares:,.0f} shares paired "
        f"({100 * 2 * paired / max(shares, 1e-9):.0f}%) at an average pair cost of {cost / max(paired, 1e-9):.3f}; "
        f"pnl locked in pairs {locked:+,.0f} $, from unpaired shares {total - locked:+,.0f} $")  # fmt: skip
    n = [len([r for r in rows if r["key"] == key]) for key in by]
    out(f"  fills per market: median {sorted(n)[len(n) // 2]}, max {max(n)}; fill size $: median "
        f"{sorted(r['usd'] for r in rows)[len(rows) // 2]:.1f}, p90 {sorted(r['usd'] for r in rows)[int(0.9 * len(rows))]:.1f}; "
        f"sells: {sum(r['side'] == 'SELL' for r in rows)}")  # fmt: skip


def _fills_summary(rows, out=print, binance=True):
    """Settled c/$ by Binance move toward the token (1 s, 5 s, since open; skipped with binance=False), by price vs mid, by role."""
    import numpy as np

    rows = [r for r in rows if r["out"] != "?" and (np.isfinite(r["mv_1s"]) or not binance)]
    if not rows:
        out("no settled rows")
        return
    pnl = np.array([r["pnl"] for r in rows])
    usd = np.array([r["usd"] for r in rows])
    out(f"settled fills {len(rows)}: pnl {pnl.sum():+,.0f} $ on ${usd.sum():,.0f} ({100 * pnl.sum() / usd.sum():+.2f} c/$), "
        f"win rate {np.mean([r['out'] == 'W' for r in rows]):.3f}")  # fmt: skip

    # how much of the result is a few markets: fills in one market share one outcome, so resample whole markets
    uk, inv = np.unique([r["key"] for r in rows], return_inverse=True)
    mp, mu = np.bincount(inv, pnl), np.bincount(inv, usd)
    pick = np.random.default_rng(0).integers(0, len(uk), (2000, len(uk)))
    lo, hi = np.percentile(100 * mp[pick].sum(1) / mu[pick].sum(1), [5, 95])
    srt = np.sort(mp)
    out(f"  by market ({len(uk)}): 90% CI {lo:+.2f} .. {hi:+.2f} c/$; best 5 markets {srt[-5:].sum():+,.0f} $, worst 5 {srt[:5].sum():+,.0f} $; "
        f"markets won {int((mp > 0).sum())}, lost {int((mp < 0).sum())}")  # fmt: skip
    days = {}
    for r, v in zip(rows, pnl):
        d = time.strftime("%m-%d", time.gmtime(r["x"] / 1000))
        days[d] = days.get(d, 0.0) + float(v)
    out("  by day ($): " + " | ".join(f"{d} {v:+,.0f}" for d, v in sorted(days.items())))

    def table(name, vals, edges, fmt):
        v = np.array(vals, float)
        cells = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (v >= lo) & (v < hi)
            if m.sum():
                lab = f"{'' if lo < -1e8 else fmt.format(lo)}..{'' if hi > 1e8 else fmt.format(hi)}"
                cells.append(f"{lab} n={int(m.sum())} {100 * pnl[m].sum() / max(usd[m].sum(), 1e-9):+.1f}c/$ ({pnl[m].sum():+,.0f}$)")
        out(f"  {name:>17}: " + " | ".join(cells))

    inf = 1e9
    if binance:
        table("bn move 1s (bp)", [r["mv_1s"] for r in rows], (-inf, -2, -0.5, 0.5, 2, 5, inf), "{:g}")
        table("bn move 5s (bp)", [r["mv_5s"] for r in rows], (-inf, -5, -2, 0, 2, 5, 10, inf), "{:g}")
        table("bn since open (bp)", [r["mv_open"] for r in rows], (-inf, -10, -5, -2, 0, 2, 5, 10, 20, inf), "{:g}")
    table("price - mid (c)", [100 * r["vs_mid"] for r in rows], (-inf, -2, -1, -0.5, 0, 0.5, 1, 2, inf), "{:g}")
    table("time left (s)", [r["left"] for r in rows], (-inf, 10, 30, 60, 120, 180, inf), "{:g}")
    table("token price", [r["p"] for r in rows], (0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.01), "{:g}")
    nan = float("nan")
    out("  Polymarket books just before the fill (the token's side):")
    table("vs best bid (ticks)", [r.get("tick", nan) for r in rows], (-inf, -1.5, -0.5, 0.5, 1.5, inf), "{:g}")
    # at the best bid: was the level thick (it joined) or about its own size (its order made the level)?
    at = [r.get("lvl", nan) if r.get("tick") == 0 else nan for r in rows]
    table("level size (sh)", at, (0, 20, 60, 200, 1000, inf), "{:g}")
    table("fill / level size", [r["sz"] / v if v == v and v > 0 else nan for r, v in zip(rows, at)], (0, 0.1, 0.5, 0.9, inf), "{:g}")
    table("spread (ticks)", [r.get("spr", nan) for r in rows], (0.5, 1.5, 2.5, 3.5, inf), "{:g}")
    table("book imbalance", [r.get("imb", nan) for r in rows], (-1.01, -0.6, -0.3, 0.3, 0.6, 1.01), "{:g}")
    table("ms best unchanged", [r.get("calm", nan) for r in rows], (0, 100, 300, 1000, 3000, inf), "{:g}")
    table("own mid move 1s (c)", [r.get("own1s", nan) for r in rows], (-inf, -1.5, -0.25, 0.25, 1.5, inf), "{:g}")
    table("alt move 500ms (c)", [r.get("alt", nan) for r in rows], (-inf, -0.75, -0.25, 0.25, 0.75, inf), "{:g}")
    _pairs_summary(rows, out)
    for role in ("taker", "maker"):
        m = np.array([r["role"] == role for r in rows])
        if m.any():
            out(f"  {role}: n={int(m.sum())} {100 * pnl[m].sum() / usd[m].sum():+.2f} c/$, median price - mid "
                f"{100 * np.median([r['vs_mid'] for r, k in zip(rows, m) if k]):+.2f}c")  # fmt: skip


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
    p = sp.add_parser("profile")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--days", nargs="*")
    p.add_argument("--asset", choices=ASSETS)
    p.add_argument("--tf", type=int, choices=(5, 15))
    p.add_argument("--wallets", nargs="*", default=[])
    p.add_argument("--settle", type=Path, default=Path.home() / "act-settlements.csv")
    p.add_argument("--no-fetch", action="store_true")
    p = sp.add_parser("fills")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--days", nargs="*")
    p.add_argument("--asset", choices=ASSETS)
    p.add_argument("--tf", type=int, choices=(5, 15))
    p.add_argument("--wallet", required=True)
    p.add_argument("--summary", action="store_true", help="settled c/$ by Binance move, price vs mid, time left")
    p.add_argument("--no-binance", action="store_true", help="do not read Binance rows; summary without the Binance cuts")
    p.add_argument("--settle", type=Path, default=Path.home() / "act-settlements.csv")
    p.add_argument("--no-fetch", action="store_true")
    p = sp.add_parser("pnl")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--days", nargs="*")
    p.add_argument("--asset", choices=ASSETS)
    p.add_argument("--tf", type=int, choices=(5, 15))
    p.add_argument("--min-usd", type=float, default=2000.0)
    p.add_argument("--min-markets", type=int, default=30)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--settle", type=Path, default=Path.home() / "act-settlements.csv")
    p.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    if a.cmd == "rec":
        asyncio.run(run(a))
    elif a.cmd == "pnl":
        cmd_pnl(a)
    elif a.cmd == "profile":
        cmd_profile(a)
    elif a.cmd == "fills":
        cmd_fills(a)
    else:
        cmd_top(a)


if __name__ == "__main__":
    main()
