"""pmlat: Polymarket-only low-latency research (overnight 2026-11-16/17, Dublin box).

Data layer: streams the shadow recordings hour by hour (never a whole day), one asset/timeframe at a time, and
builds dsim markets (same print match-time model, same taker/maker fill rules: ~/dublin-retest/repo/dsim.py).
Each print gets its taker wallet from the RTDS census (act rows, fee > 0) when the tx is found.

Latency scenarios (ms, exchange clock), dsim conventions:
  dublin: book/print seen +9, order on book +22 (probe p50 send->visible), cancel +21, taker hold 50
  tokyo : book +116, order +120, cancel +120, hold 50
  dublin_p90 (stress): book +23, order +52, cancel +69, hold 50
"""

import csv
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path.home() / "dublin-retest" / "repo"))
import dsim  # noqa: E402

from act_rec import ACT_DT  # noqa: E402

REC = Path.home() / "shadow-rec"
OUT = Path(__file__).resolve().parent / "out" / "pmlat"
WIN = OUT / "winners.csv"
ADT = np.dtype(ACT_DT)
SC = {
    "dublin": dict(book=9.0, order=22.0, cancel=21.0, hold=50.0),
    "tokyo": dict(book=116.0, order=120.0, cancel=120.0, hold=50.0),
    # stress: this box's p90-ish delays (receive p90 ~23 ms; probe send->visible p90 33-95 ms by hour; cancel p90 54-69 ms in good hours)
    "dublin_p90": dict(book=23.0, order=52.0, cancel=69.0, hold=50.0),
    # measured live on dublin 2 (2026-11-17), first version: order +200, cancel +130, taker hold 150 (files f7i-live_ack200-*)
    "live_ack200": dict(book=9.0, order=200.0, cancel=130.0, hold=150.0),
    # measured live on dublin 2 (2026-11-17), second version
    "live_ack": dict(book=9.0, order=250.0, cancel=130.0, hold=150.0),
    # state-dependent post latency: +55 if the BTC 5m best bid and ask were unchanged over the previous 1 s, else +157
    # (order = the slow value, so scripts that ignore order_fast are conservative); see olat()
    "live_lean": dict(book=9.0, order=157.0, order_fast=55.0, stable_ms=1000.0, cancel=136.0, hold=150.0),
    "live_hold150": dict(book=9.0, order=22.0, cancel=21.0, hold=150.0),
}
MAXDAY = os.environ.get("PMLAT_MAXDAY")  # e.g. 2026-11-16: hour h+1 of the last hour would otherwise read the next day
DEV = ("2026-11-14", "2026-11-15")
HOLD = ("2026-11-16",)


def _hour_file(day, h, kind, dt, a, tf):
    from datetime import date, timedelta

    d = date.fromisoformat(day)
    if h < 0:
        d, h = d - timedelta(days=1), h + 24
    elif h > 23:
        d, h = d + timedelta(days=1), h - 24
    if MAXDAY and d.isoformat() > MAXDAY:  # hard cap: never read a recording day after MAXDAY
        return np.zeros(0, dt)
    f = REC / d.isoformat() / f"{kind}-{h:02d}.bin"
    if not f.exists():
        return np.zeros(0, dt)
    x = np.fromfile(f, dtype=dt)
    return x[(x["a"] == a) & (x["tf"] == tf)]


def hours(day, asset="btc", tf=5, hrs=range(24), with_act=True):
    """Yield (hour, {epoch: market}) for markets whose window starts in that hour. Each market dict also has
    'twallet' (per print, taker wallet hex or '') and 'act' (census rows for the market)."""
    from datetime import datetime, timezone

    a = dsim.ASSETS.index(asset)
    cache = {}

    def get(kind, dt, h):
        k = (kind, h)
        if k not in cache:
            cache[k] = _hour_file(day, h, kind, dt, a, tf)
        return cache[k]

    d0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    for h in hrs:
        for k in [k for k in cache if k[1] < h - 1]:
            del cache[k]
        H0, H1 = d0 + 3600 * h, d0 + 3600 * (h + 1)
        cat = lambda kind, dt: np.concatenate([get(kind, dt, hh) for hh in (h - 1, h, h + 1)])  # noqa: E731
        pm, tr = cat("pm", dsim.PM_DT), cat("tr", dsim.TR_DT)
        act = cat("act", ADT) if with_act else np.zeros(0, ADT)
        pm, tr, act = (z[(z["e"] >= H0) & (z["e"] < H1)] for z in (pm, tr, act))
        if not len(pm):
            continue
        tw = {}
        if len(act):
            tk = act[act["fee"] > 0]
            tw = {bytes(t): w.hex() for t, w in zip(tk["tx"], tk["w"])}
        out = {}
        for e in np.unique(pm["e"]):
            m = dsim.mk_market(pm[pm["e"] == e], tr[tr["e"] == e])
            m["key"] = (asset, tf, int(e))
            m["twallet"] = np.array([tw.get(bytes(t), "") for t in m["pr"]["tx"]]) if m["pr"]["tx"] is not None else None
            m["act"] = act[act["e"] == e]
            out[int(e)] = m
        yield h, out


def slug(k):
    return f"{k[0]}-updown-{k[1]}m-{k[2]}"


def winners(slugs=()):
    """slug -> 'UP'/'DOWN'; seeds from ~/dublin-retest/winners.csv (read only), fetches the rest from Gamma."""
    OUT.mkdir(parents=True, exist_ok=True)
    have = {}
    for f in (Path.home() / "dublin-retest" / "winners.csv", WIN):
        if f.exists():
            have.update({r["slug"]: r["winner"] for r in csv.DictReader(open(f))})
    todo = [s for s in slugs if s not in have]
    if todo:
        new = not WIN.exists()
        with open(WIN, "a") as f:
            if new:
                f.write("slug,winner\n")
            for i in range(0, len(todo), 20):
                q = "&".join(f"slug={s}" for s in todo[i : i + 20])
                req = urllib.request.Request(
                    f"https://gamma-api.polymarket.com/markets?closed=true&{q}", headers={"User-Agent": "research"}
                )
                try:
                    res = json.load(urllib.request.urlopen(req, timeout=20))
                except Exception as e:  # noqa: BLE001
                    print("gamma error", e, file=sys.stderr)
                    continue
                for mk in res:
                    try:
                        pr, names = json.loads(mk["outcomePrices"]), json.loads(mk["outcomes"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if mk.get("closed") and "1" in pr:
                        w = names[pr.index("1")].upper()
                        f.write(f"{mk['slug']},{w}\n")
                        have[mk["slug"]] = w
                time.sleep(0.2)
    return have


def olat(m, t, sc):
    """Order latency (ms) for a post sent at exchange time t: sc['order'], or sc['order_fast'] when the market's best
    bid and ask did not change in (t - stable_ms, t] (live_lean)."""
    if "order_fast" not in sc:
        return sc["order"]
    B = m["book"]
    i = int(np.searchsorted(B["x"], t, "right")) - 1
    j = int(np.searchsorted(B["x"], t - sc["stable_ms"], "right")) - 1
    if j < 0 or i < 0:
        return sc["order"]
    b, k = B["b"][j : i + 1], B["k"][j : i + 1]
    stable = b[0] > 0 and k[0] > 0 and (b == b[0]).all() and (k == k[0]).all()
    return sc["order_fast"] if stable else sc["order"]


def mid_up(m, t):
    """UP mid (1e-4) of the book in force at t (inclusive), None if unknown."""
    return dsim.mid(m, t, 0)


def boot_ci(vals, groups, B=2000, seed=0, weights=None):
    """90% CI of sum(vals)/sum(weights) resampling groups (market or day). vals/weights per fill."""
    vals = np.asarray(vals, float)
    w = np.ones(len(vals)) if weights is None else np.asarray(weights, float)
    g = np.asarray(groups)
    u, inv = np.unique(g, return_inverse=True)
    if len(u) < 2:
        return (np.nan, np.nan)
    S = np.bincount(inv, vals, len(u))
    N = np.bincount(inv, w, len(u))
    i = np.random.default_rng(seed).integers(0, len(u), (B, len(u)))
    with np.errstate(invalid="ignore", divide="ignore"):
        m = S[i].sum(1) / N[i].sum(1)
    return tuple(np.nanpercentile(m, [5, 95]))
