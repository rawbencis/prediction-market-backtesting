"""pmlat: Polymarket-only low-latency research (overnight 2026-11-16/17, Dublin box).

Data layer: streams the shadow recordings hour by hour (never a whole day), one asset/timeframe at a time, and
builds dsim markets (same print match-time model, same taker/maker fill rules: ~/dublin-retest/repo/dsim.py).
Each print gets its taker wallet from the RTDS census (act rows, fee > 0) when the tx is found.

Latency scenarios (ms, exchange clock), dsim conventions:
  dublin: book/print seen +9, order on book +22 (probe p50 send->visible), cancel +21, taker hold 50
  tokyo : book +116, order +120, cancel +120, hold 50
"""

import csv
import json
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
}
DEV = ("2026-11-14", "2026-11-15")
HOLD = ("2026-11-16",)


def _hour_file(day, h, kind, dt, a, tf):
    from datetime import date, timedelta

    d = date.fromisoformat(day)
    if h < 0:
        d, h = d - timedelta(days=1), h + 24
    elif h > 23:
        d, h = d + timedelta(days=1), h - 24
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
