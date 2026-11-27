"""PF1 simulation engine: Lighter x Polymarket BTC 15m pair, books from Dublin (Polymarket) and Tokyo (Lighter).

Clocks. Every book row carries its venue's exchange time (ms). Polymarket rows also carry Dublin's receive time `r`;
Lighter rows carry Tokyo's receive time. All matching is done in exchange time (venue clocks taken as UTC, as the
recorders' NTP-synced boxes see them; offsets are reported by clock_check.py).

  book at exchange time t  : last row with exchange time <= t (a matching engine sees this)
  view at local time t     : last row whose (exchange time + view lag) <= t, or for Polymarket at Dublin, r <= t

Prices. Polymarket keeps the Up token book only (Down = mirror: Down ask = 1 - Up bid, size = Up bid size).
Lighter keeps the Up book (selling Up at the bid = buying Down at 1 - bid). Fees: Polymarket rate(window) * p(1-p)
per share at each fill price; Lighter 0.01 * p(1-p).

Accounting. Each attempt books every leg as a position in Up or Down shares with its cash cost and fee; unwinds
book the proceeds; the window's outcome settles whatever is left (a pair Up + Down pays $1). So pairs, unwinds and
residuals held to settlement all go through one P&L. Orders below 5 shares cannot be sent on either venue: such
residuals are held to settlement.
"""
import csv
import gzip
import math
from bisect import bisect_right
from pathlib import Path

import numpy as np

HOME = Path.home()
CACHE = HOME / "pf1-cache"
LIMPORT = HOME / "lighter-import"
L_FEE = 0.01
MIN_SZ = 5.0
FEE_SWITCH_E = 1795219200  # 2026-11-21 00:00 UTC (census: every 11-21 window at 0.05)


def fl(p):
    return L_FEE * p * (1 - p)


# ------------------------------------------------------------------ static data
class Static:
    def __init__(self, census=None):
        self.out = {}  # window epoch -> True if Up won (Lighter outcome 2 = Up, 1 = Down)
        mk = {r["market_id"]: int(r["window_start"]) for r in csv.DictReader(open(LIMPORT / "markets.csv"))
              if r["asset"] == "btc" and r["tf"] == "15"}
        for r in csv.DictReader(open(LIMPORT / "outcomes.csv")):
            e = mk.get(r["market_id"])
            if e is not None and r["outcome"] in ("1", "2"):
                self.out[e] = r["outcome"] == "2"
        self.fee = {}
        if census and Path(census).exists():
            from datetime import datetime, timezone
            for r in csv.DictReader(open(census)):
                if r["asset"] == "btc" and r["tf"] == "15m" and r["fee_rate"]:
                    e = int(datetime.strptime(r["window_start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
                    self.fee[e] = float(r["fee_rate"])

    def rate(self, e):
        if e in self.fee:
            return self.fee[e], "census"
        return (0.05 if e >= FEE_SWITCH_E else 0.07), "rule"


# ------------------------------------------------------------------ books
class Side:
    """One venue's Up book for one window: arrays sorted by exchange time, with a view index."""

    def __init__(self, tx, bp, bs, ap, as_, view=None):
        o = np.argsort(tx, kind="stable")
        self.tx = np.asarray(tx, dtype=np.float64)[o]
        self.bp, self.bs, self.ap, self.as_ = (np.asarray(a, dtype=np.float64)[o] for a in (bp, bs, ap, as_))
        self.view_raw = None if view is None else np.asarray(view, dtype=np.float64)[o]
        self._txl = self.tx.tolist()

    def at(self, t):
        """Index of the book in force at exchange time t (-1 if none)."""
        return bisect_right(self._txl, t) - 1

    def view_times(self, lag):
        """Local times at which each row becomes visible: exchange + lag, or the recorded receive time (lag None)."""
        if lag is None:
            v = np.maximum.accumulate(self.view_raw)  # a row is seen only after the rows before it
        else:
            v = self.tx + lag
        return v

    def valid(self, i):
        if i < 0:
            return False
        b, a = self.bp[i, 0], self.ap[i, 0]
        return b > 0 and a > b and not (math.isnan(b) or math.isnan(a))


class ViewIdx:
    def __init__(self, side, lag):
        self.v = side.view_times(lag)
        self._vl = self.v.tolist()

    def at(self, t):
        return bisect_right(self._vl, t) - 1


def load_lighter(day, windows=None):
    """-> {window epoch: Side} from ~/lighter-import/DAY.csv.gz (top 3, Tokyo receive)."""
    fp = LIMPORT / f"{day}.csv.gz"
    if not fp.exists():
        return {}
    import pandas as pd
    cols = ["t_exch_ms", "t_recv_ms", "window_start"] + [f"{k}{j}" for j in (1, 2, 3) for k in ("bid", "bidsz", "ask", "asksz")]
    parts = []
    for ch in pd.read_csv(fp, usecols=cols, chunksize=500_000):  # chunked: only the wanted windows are kept
        if windows is not None:
            ch = ch[ch.window_start.isin(windows)]
        if len(ch):
            parts.append(ch)
    if not parts:
        return {}
    df = pd.concat(parts, ignore_index=True)
    del parts
    out = {}
    for e, g in df.groupby("window_start", sort=False):
        e = int(e)
        if windows is not None and e not in windows:
            continue
        bp = g[["bid1", "bid2", "bid3"]].to_numpy(float)
        bs = g[["bidsz1", "bidsz2", "bidsz3"]].to_numpy(float)
        ap = g[["ask1", "ask2", "ask3"]].to_numpy(float)
        as_ = g[["asksz1", "asksz2", "asksz3"]].to_numpy(float)
        bp[bp <= 0] = np.nan
        ap[ap <= 0] = np.nan
        bs[np.isnan(bp)] = 0
        as_[np.isnan(ap)] = 0
        out[e] = Side(g["t_exch_ms"].to_numpy(float), bp, bs, ap, as_, view=g["t_recv_ms"].to_numpy(float))
    return out


def pm_hours(day):
    d = CACHE / day
    return sorted(int(p.stem[3:5]) for p in d.glob("pm-*.npz"))


def load_pm_hour(day, hh):
    fp = CACHE / day / f"pm-{hh:02d}.npz"
    if not fp.exists():
        return None
    z = np.load(fp)
    return {k: z[k] for k in ("r", "x", "e", "bp", "bs", "ap", "as", "tr")} | {"source": str(z["source"])}


def pm_side(chunks, e):
    """Side for window e from a list of hour chunks. Drop rows (x == 0) get x = r - the window's median lag."""
    sel = [(c, c["e"] == e) for c in chunks if c is not None]
    r = np.concatenate([c["r"][m] for c, m in sel])
    if len(r) == 0:
        return None
    x = np.concatenate([c["x"][m] for c, m in sel]).astype(np.float64)
    bp, bs, ap, as_ = (np.concatenate([c[k][m] for c, m in sel]).astype(np.float64) for k in ("bp", "bs", "ap", "as"))
    o = np.argsort(r, kind="stable")
    r, x, bp, bs, ap, as_ = r[o], x[o], bp[o], bs[o], ap[o], as_[o]
    good = x > 0
    lag = float(np.median(r[good] - x[good])) if good.any() else 10.0
    x[~good] = r[~good] - lag
    # exchange stamps are ms and can tie or step back by a few ms across batches; keep the receive order for ties
    x = np.maximum.accumulate(x)
    bs = np.nan_to_num(bs)
    as_ = np.nan_to_num(as_)
    sd = Side(x, bp, bs, ap, as_, view=r)
    tr = np.concatenate([c["tr"][c["tr"][:, 2] == e] for c in chunks if c is not None and len(c["tr"])] or [np.zeros((0, 7))])
    sd.trades = Trades(tr[np.argsort(tr[:, 1], kind="stable")])
    return sd


class Trades:
    """Polymarket trades of one window (Dublin feed): exchange ms x, token (0 Up / 1 Down), taker side (0 BUY / 1 SELL),
    price, size. `own` marks our own fills (V0 only), which are not competitors."""

    def __init__(self, tr):
        self.x, self.tok, self.side, self.p, self.sz = tr[:, 1], tr[:, 3], tr[:, 4], tr[:, 5], tr[:, 6]
        self.own = np.zeros(len(self.x), dtype=bool)

    def taken(self, token, buy, limit, t0, t1):
        """Shares other takers took between exchange times (t0, t1] from the liquidity our order would take, priced in
        our token: buying `token` at <= limit competes with takers buying it (p <= limit) and takers selling the other
        token (1 - p <= limit), which hit the mirrored book; selling at >= limit, the reverse."""
        if len(self.x) == 0 or t1 <= t0:
            return 0.0
        i0, i1 = np.searchsorted(self.x, t0, "right"), np.searchsorted(self.x, t1, "right")
        if i1 <= i0:
            return 0.0
        sl = slice(i0, i1)
        tk = 0 if token == "up" else 1
        same, other = self.tok[sl] == tk, self.tok[sl] != tk
        px = np.where(same, self.p[sl], 1 - self.p[sl])  # price in our token
        if buy:
            m = ((same & (self.side[sl] == 0)) | (other & (self.side[sl] == 1))) & (px <= limit + 1e-9)
        else:
            m = ((same & (self.side[sl] == 1)) | (other & (self.side[sl] == 0))) & (px >= limit - 1e-9)
        m &= ~self.own[sl]
        return float(self.sz[sl][m].sum())


NO_TRADES = Trades(np.zeros((0, 7)))


# ------------------------------------------------------------------ fills (strict: displayed size at <= limit)
def walk(prices, sizes, q, ok, taken=0.0):
    """Fill up to q over levels where ok(price), after removing `taken` shares (other takers ahead of us) from the
    best levels first; -> (filled, [(px, sz), ...])."""
    got, fills = 0.0, []
    for p, s in zip(prices, sizes):
        if taken > 0 and not math.isnan(p) and s > 0 and ok(p):
            cut = min(s, taken)
            s, taken = s - cut, taken - cut
        if got >= q - 1e-9:
            break
        if math.isnan(p) or s <= 0 or not ok(p):
            if math.isnan(p) or not ok(p):
                break
            continue
        take = min(s, q - got)
        fills.append((float(p), float(take)))
        got += take
    return got, fills


def pm_buy(side, i, token, limit, q, taken=0.0):
    """Buy `token` ('up'|'down') on Polymarket at book index i, limit price, up to q; `taken` = competitor shares
    removed from the best levels (Trades.taken)."""
    if i < 0:
        return 0.0, []
    if token == "up":
        return walk(side.ap[i], side.as_[i], q, lambda p: p <= limit + 1e-9, taken)
    return walk(1 - side.bp[i], side.bs[i], q, lambda p: p <= limit + 1e-9, taken)


def pm_sell(side, i, token, q, floor=0.0, taken=0.0):
    if i < 0:
        return 0.0, []
    if token == "up":
        return walk(side.bp[i], side.bs[i], q, lambda p: p >= floor - 1e-9, taken)
    return walk(1 - side.ap[i], side.as_[i], q, lambda p: p >= floor - 1e-9, taken)


def pm_fill(side, token, buy, limit, q, t_arrive, t_match, comp=True, stats=None):
    """Polymarket FAK arriving at exchange time t_arrive, matched at t_match on the book then in force, with the
    competitor term: shares other takers took at our limit or better in (t_arrive, t_match] are removed first.
    stats (dict, optional) accumulates 'taken' (removed) and 'book_drop' (size at our limit or better that the
    displayed book itself lost between arrival and match, i.e. what the book already reflected)."""
    i = side.at(t_match)
    tr = getattr(side, "trades", NO_TRADES)
    tk = tr.taken(token, buy, limit, t_arrive, t_match) if comp else 0.0
    if stats is not None:
        stats["taken"] = stats.get("taken", 0.0) + tk
        stats["book_drop"] = stats.get("book_drop", 0.0) + max(0.0, avail(side, side.at(t_arrive), token, buy, limit)
                                                                 - avail(side, i, token, buy, limit))
        stats["orders"] = stats.get("orders", 0) + 1
        stats["orders_hit"] = stats.get("orders_hit", 0) + (tk > 0)
        if tk > 0:
            f0 = (pm_buy(side, i, token, limit, q) if buy else pm_sell(side, i, token, q, limit))[0]
            f1 = (pm_buy(side, i, token, limit, q, tk) if buy else pm_sell(side, i, token, q, limit, tk))[0]
            stats["fill_lost"] = stats.get("fill_lost", 0.0) + f0 - f1
            stats["orders_lost"] = stats.get("orders_lost", 0) + (f0 > 0 and f1 == 0)
    if buy:
        return pm_buy(side, i, token, limit, q, tk)
    return pm_sell(side, i, token, q, limit, tk)


def avail(side, i, token, buy, limit):
    """Displayed size at `limit` or better over the top 3 levels for our order."""
    if i < 0:
        return 0.0
    if buy:
        px, sz = (side.ap[i], side.as_[i]) if token == "up" else (1 - side.bp[i], side.bs[i])
        return walk(px, sz, 1e18, lambda p: p <= limit + 1e-9)[0]
    px, sz = (side.bp[i], side.bs[i]) if token == "up" else (1 - side.ap[i], side.as_[i])
    return walk(px, sz, 1e18, lambda p: p >= limit - 1e-9)[0]


def l_trade(side, i, buy_up, limit, q):
    """Lighter IOC: buy Up at asks <= limit, or sell Up at bids >= limit."""
    if i < 0:
        return 0.0, []
    if buy_up:
        return walk(side.ap[i], side.as_[i], q, lambda p: p <= limit + 1e-9)
    return walk(side.bp[i], side.bs[i], q, lambda p: p >= limit - 1e-9)


class Book:
    """Positions and cash of one attempt."""

    def __init__(self, rate):
        self.rate, self.up, self.dn, self.cash, self.fees = rate, 0.0, 0.0, 0.0, 0.0

    def pm(self, token, fills, sell=False):
        for p, s in fills:
            f = self.rate * p * (1 - p) * s
            self.fees += f
            sgn = -1 if sell else 1
            self.cash -= sgn * p * s + f
            if token == "up":
                self.up += sgn * s
            else:
                self.dn += sgn * s

    def lighter(self, buy_up, fills):
        for p, s in fills:
            f = fl(p) * s
            self.fees += f
            if buy_up:
                self.cash -= p * s + f
                self.up += s
            else:  # sell Up at p == hold a Down share bought at 1 - p (fully collateralised)
                self.cash -= (1 - p) * s + f
                self.dn += s

    def lighter_close(self, buy_up_was, fills):
        """Unwind a Lighter position: sell Up back (if we bought Up) or buy Up back (if we sold Up)."""
        for p, s in fills:
            f = fl(p) * s
            self.fees += f
            if buy_up_was:
                self.cash += p * s - f
                self.up -= s
            else:
                self.cash += (1 - p) * s - f
                self.dn -= s

    def settle(self, up_won):
        return self.cash + (self.up if up_won else self.dn)


def vwap(fills):
    s = sum(z for _, z in fills)
    return sum(p * z for p, z in fills) / s if s else float("nan")
