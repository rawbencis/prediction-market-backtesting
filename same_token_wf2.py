"""Same-token maker round-trip research on BTC 5m up/down top-of-book Parquet.

Quote-proxy execution (trade-through fills, fixed latency), not a native L2/queue backtest.
Stages: prep (Parquet -> per-day npz cache), settle (Gamma winners), grid (all candidates,
all markets), wf (walk-forward selection + frozen holdout).
"""

import argparse
import csv
import itertools
import json
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import numpy as np

SIDES = ("UP", "DOWN")
MKT_US = 300_000_000
TRIG_MAX_US = 240_000_000
LAT_US = 250_000
GAP_US = 5_000_000
AGE_US = 2_000_000
SIZE = 5.0  # Polymarket minimum order size; displayed size must cover it
GRID = {
    "x": (550, 600, 650, 700, 750, 800, 850),  # trigger mid, milli-dollars
    "b": (150, 200, 250, 300, 350, 400, 450),  # resting maker buy
    "s": (450, 500, 550, 600, 650, 700, 750, 800, 850),  # resting maker sell
    "ttl": (30, 60, 120, 300),  # entry order lifetime, s
}
PRIOR = (700, 300, 700, 60)  # pre-registered idea, reported without selection
# status codes
FLAT, EXPIRED, OPEN_END, EXITED, SETTLED, UNSETTLED, CENS_PEND, CENS_POS = range(8)
STATUS = (
    "flat",
    "expired",
    "pending_at_end",
    "exited",
    "settled",
    "unsettled",
    "censored_pending",
    "censored_position",
)


def candidates():
    return [
        c
        for c in itertools.product(GRID["x"], GRID["b"], GRID["s"], GRID["ttl"])
        if c[1] < c[0] and c[1] < c[2]
    ]


def day_of(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()


# ---------------------------------------------------------------- prep


def _num(col):
    import pyarrow as pa
    import pyarrow.compute as pc

    try:
        return np.asarray(pc.cast(col, pa.float64()).to_numpy(), float)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        out = []
        for v in col.to_pylist():
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(np.nan)
        return np.array(out, float)


def side_rows(ts, rx, q):
    """Receipt-ordered, one row per receipt time. q: (n,4) bid, ask, bid_size, ask_size."""
    ok = (
        np.isfinite(q).all(1)
        & (q[:, 0] >= 0)
        & (q[:, 0] < q[:, 1])
        & (q[:, 1] <= 1)
        & (q[:, 2] > 0)
        & (q[:, 3] > 0)
    )
    o = np.lexsort((ts, rx))
    ts, rx, q, ok = ts[o], rx[o], q[o], ok[o]
    n = len(rx)
    if not n:
        return rx, q, ok, 0
    start = np.r_[True, rx[1:] != rx[:-1]]
    gid = np.cumsum(start) - 1
    last = np.r_[np.flatnonzero(start)[1:] - 1, n - 1]
    L = last[gid]
    qz = np.where(ok[:, None], q, 0.0)
    differ = (ok != ok[L]) | (qz != qz[L]).any(1)
    conflict = np.bincount(gid, (ts == ts[L]) & differ, len(last)) > 0
    return rx[last], q[last], ok[last] & ~conflict, int(conflict.sum())


def merge(epoch, sides):
    """sides: [(rx, q, ok)] per side -> event arrays inside [epoch, epoch+300s)."""
    e0 = epoch * 1_000_000
    T = np.unique(np.concatenate([s[0] for s in sides]))
    T = T[(T >= e0) & (T < e0 + MKT_US)]
    n = len(T)
    bid = np.full((n, 2), -1, np.int32)
    ask = np.full((n, 2), -1, np.int32)
    bsz = np.zeros((n, 2), np.float32)
    asz = np.zeros((n, 2), np.float32)
    upd = np.zeros((n, 2), bool)
    seen = np.full((n, 2), np.iinfo(np.int64).min // 2, np.int64)
    for k, (rx, q, ok) in enumerate(sides):
        if not len(rx):
            continue
        i = np.searchsorted(rx, T, "right") - 1
        has = i >= 0
        ii = np.maximum(i, 0)
        v = has & ok[ii]
        upd[:, k] = has & (rx[ii] == T)
        seen[has, k] = rx[ii[has]] - e0
        bid[v, k] = np.rint(q[ii[v], 0] * 1000)
        ask[v, k] = np.rint(q[ii[v], 1] * 1000)
        bsz[v, k] = q[ii[v], 2]
        asz[v, k] = q[ii[v], 3]
    return {"t": T - e0, "bid": bid, "ask": ask, "bsz": bsz, "asz": asz, "upd": upd, "seen": seen}


def load_market(args):
    data, epoch = args
    import pyarrow.parquet as pq

    sides, diag = [], Counter()
    for side in SIDES:
        p = data / f"btc-updown-5m-{epoch}_{side}.parquet"
        if not p.is_file():
            return epoch, None, {"missing": 1}
        try:
            tb = pq.read_table(
                p,
                columns=[
                    "timestamp_us",
                    "local_timestamp_us",
                    "bid_price",
                    "ask_price",
                    "bid_size",
                    "ask_size",
                ],
            )
            ts = tb["timestamp_us"].to_numpy().astype(np.int64)
            rx = tb["local_timestamp_us"].to_numpy().astype(np.int64)
            q = np.column_stack(
                [_num(tb[c]) for c in ("bid_price", "ask_price", "bid_size", "ask_size")]
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not kill the run
            return epoch, None, {"error": 1, "msg": f"{p.name}: {e!r}"[:300]}
        rx_u, q_u, ok_u, nconf = side_rows(ts, rx, q)
        diag["rows"] += len(rx)
        diag["conflicts"] += nconf
        diag["invalid"] += int((~ok_u).sum())
        sides.append((rx_u, q_u, ok_u))
    m = merge(epoch, sides)
    diag["events"] = len(m["t"])
    return epoch, m, dict(diag)


KEYS = ("t", "bid", "ask", "bsz", "asz", "upd", "seen")


def prep_market(args):
    """Worker: load one market and write it straight to disk; only diagnostics go back."""
    data, epoch, ddir = args
    _, m, diag = load_market((data, epoch))
    if m is not None:
        tmp = ddir / f".{epoch}.tmp"
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, **m)
        tmp.replace(ddir / f"{epoch}.npz")
    return diag


def markets(ddir):
    for f in sorted(ddir.glob("[0-9]*.npz")):
        with np.load(f) as z:
            yield int(f.stem), {k: z[k] for k in KEYS}


def done_days(cache):
    return sorted(p.parent for p in cache.glob("????-??-??/_done.json"))


def cmd_prep(a):
    epochs = sorted(
        int(f.stem.split("_")[0].rsplit("-", 1)[1])
        for f in a.data.glob("btc-updown-5m-*_UP.parquet")
    )
    if not epochs:
        sys.exit("no UP Parquet files")
    days = {}
    for e in epochs:
        days.setdefault(day_of(e), []).append(e)
    with Pool(a.workers, maxtasksperchild=200) as pool:
        for d, es in sorted(days.items()):
            ddir = a.cache / d
            if (ddir / "_done.json").exists() and not a.force:
                continue
            ddir.mkdir(parents=True, exist_ok=True)
            diag, errors = Counter(), []
            for dg in pool.imap(prep_market, [(a.data, e, ddir) for e in es], chunksize=8):
                errors += [dg.pop("msg")] if "msg" in dg else []
                diag.update(dg)
            diag.update(markets=len(list(ddir.glob("[0-9]*.npz"))), scheduled=288)
            (ddir / "_done.json").write_text(
                json.dumps({"diag": diag, "errors": errors[:50]}, indent=1)
            )
            print(d, dict(diag), flush=True)


# ---------------------------------------------------------------- settle


def cmd_settle(a):
    out = a.cache / "settlements.csv"
    have = {}
    if out.exists():
        have = {r["slug"]: r["winner"] for r in csv.DictReader(out.open())}
    slugs = []
    for ddir in done_days(a.cache):
        slugs += [f"btc-updown-5m-{f.stem}" for f in sorted(ddir.glob("[0-9]*.npz"))]
    todo = [s for s in slugs if s not in have]
    print(f"{len(slugs)} markets, {len(todo)} to fetch", flush=True)

    def get(batch):
        url = "https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(
            f"slug={s}" for s in batch
        )
        req = urllib.request.Request(url, headers={"User-Agent": "research-backtest"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def parse(rows, want, new):
        for m in rows:
            try:
                names = [n.upper() for n in json.loads(m["outcomes"])]
                px = [float(v) for v in json.loads(m["outcomePrices"])]
            except (KeyError, TypeError, ValueError):
                continue
            if (
                m.get("slug") in want
                and m.get("closed")
                and sorted(names) == ["DOWN", "UP"]
                and max(px) >= 0.99
                and min(px) <= 0.01
            ):
                new[m["slug"]] = names[px.index(max(px))]

    new = {}
    for i in range(0, len(todo), 50):
        batch = todo[i : i + 50]
        try:
            parse(get(batch), set(batch), new)
            for sl in [x for x in batch if x not in new]:  # multi-slug fallback
                parse(get([sl]), {sl}, new)
        except Exception as e:  # noqa: BLE001
            print("fetch failed", i, repr(e), flush=True)
        print(f"{i + len(batch)}/{len(todo)} resolved={len(new)}", flush=True)
    have.update(new)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "winner"])
        w.writerows(sorted(have.items()))
    print(f"wrote {out}: {len(have)} winners; unresolved {len(set(slugs) - set(have))}")


def settlements(cache):
    p = cache / "settlements.csv"
    if not p.exists():
        return {}
    return {
        int(r["slug"].rsplit("-", 1)[1]): SIDES.index(r["winner"]) for r in csv.DictReader(p.open())
    }


# ---------------------------------------------------------------- grid


def _first(idx, lo):
    """First element of sorted idx >= lo, else big."""
    k = np.searchsorted(idx, lo)
    return int(idx[k]) if k < len(idx) else 1 << 60


def evaluate(m, grid, winner=None, lat=LAT_US, gap=GAP_US, age=AGE_US, size=SIZE):
    """-> status int8[n], lo/hi float per share*size, entry_us (-1 none)."""
    t = m["t"]
    n, nc = len(t), len(grid)
    status = np.zeros(nc, np.int8)
    lo = np.zeros(nc)
    hi = np.zeros(nc)
    entry = np.full(nc, -1, np.int64)
    if n == 0:
        return status, lo, hi, entry
    bid, ask, bsz, asz, upd = m["bid"], m["ask"], m["bsz"], m["asz"], m["upd"]
    gs = (bid >= 0) & ((t[:, None] - m["seen"]) <= age)  # per-side good quote
    good = gs.all(1)  # trigger needs both books
    gapi = np.diff(t, prepend=t[0]) > gap
    gapi[0] = t[0] > gap
    # censoring looks only at the traded side; a transient invalid tick is skipped
    lastg = np.maximum.accumulate(np.where(gs, t[:, None], np.iinfo(np.int64).min // 2), 0)
    badm = gapi[:, None] | (~gs & (t[:, None] - lastg > age) & (t[:, None] > age))
    bad = [np.flatnonzero(badm[:, k]) for k in (0, 1)]
    tail_bad = MKT_US - t[-1] > gap
    msum = bid + ask  # 2*mid in milli
    okc = np.r_[False, good[1:] & good[:-1] & ~gapi[1:] & (t[1:] <= TRIG_MAX_US)]
    usable = gs & ~gapi[:, None]
    ent = {}
    ext = {}
    trig = {}
    for x in {c[0] for c in grid}:
        cr = np.zeros((n, 2), bool)
        cr[1:] = (msum[:-1] < 2 * x) & (2 * x <= msum[1:])
        cr &= okc[:, None]
        anyc = np.flatnonzero(cr.any(1))
        if not len(anyc) or cr[anyc[0]].all():
            trig[x] = None
        else:
            trig[x] = (int(anyc[0]), int(np.argmax(cr[anyc[0]])))

    def eidx(sd, b):
        if (sd, b) not in ent:
            ent[sd, b] = np.flatnonzero(
                upd[:, sd] & usable[:, sd] & (ask[:, sd] < b) & (asz[:, sd] >= size)
            )
        return ent[sd, b]

    def xidx(sd, s):
        if (sd, s) not in ext:
            ext[sd, s] = np.flatnonzero(
                upd[:, sd] & usable[:, sd] & (bid[:, sd] > s) & (bsz[:, sd] >= size)
            )
        return ext[sd, s]

    BIG = 1 << 60
    for ci, (x, b, s, ttl) in enumerate(grid):
        tr = trig[x]
        if tr is None:
            continue
        ti, sd = tr
        T = t[ti]
        W = None if winner is None else float(winner == sd)
        B, S = b / 1000, s / 1000
        fb = _first(bad[sd], ti + 1)
        j = _first(eidx(sd, b), max(ti + 1, int(np.searchsorted(t, T + lat))))
        xi = int(np.searchsorted(t, T + ttl * 1_000_000))
        xi = max(xi, ti + 1)
        xi = xi if xi < n else BIG
        j = j if j < n else BIG
        if fb <= min(xi, j) and fb < BIG:
            status[ci] = CENS_PEND
            outs = [0.0, S - B] + ([W - B] if W is not None else [-B, 1 - B])
            lo[ci], hi[ci] = min(outs), max(outs)
            continue
        if xi <= j and xi < BIG:
            status[ci] = EXPIRED
            continue
        if j == BIG:
            if tail_bad:
                status[ci] = CENS_PEND
                outs = [0.0, S - B] + ([W - B] if W is not None else [-B, 1 - B])
                lo[ci], hi[ci] = min(outs), max(outs)
            else:
                status[ci] = OPEN_END
            continue
        entry[ci] = t[j]
        k = _first(xidx(sd, s), max(j + 1, int(np.searchsorted(t, t[j] + lat))))
        fb2 = _first(bad[sd], j + 1)
        k = k if k < n else BIG
        held = [W - B] if W is not None else [-B, 1 - B]
        if (fb2 <= k and fb2 < BIG) or (k == BIG and tail_bad):
            status[ci], outs = CENS_POS, [S - B] + held  # unobserved path: exit or hold
        elif k < BIG:
            status[ci], outs = EXITED, [S - B]
        else:
            status[ci], outs = (SETTLED if W is not None else UNSETTLED), held
        lo[ci], hi[ci] = min(outs), max(outs)
    return status, lo * size, hi * size, entry


def _grid_day(args):
    ddir, out, sett = args
    grid = candidates()
    rows = []
    for e, m in markets(ddir):
        st, lo, hi, entry = evaluate(m, grid, sett.get(e))
        rows.append((e, st, lo, hi, entry))
    if not rows:
        return ddir.name, 0
    np.savez_compressed(
        out / f"{ddir.name}.npz",
        epochs=np.array([r[0] for r in rows]),
        status=np.stack([r[1] for r in rows]),
        lo=np.stack([r[2] for r in rows]).astype(np.float32),
        hi=np.stack([r[3] for r in rows]).astype(np.float32),
        entered=np.stack([r[4] >= 0 for r in rows]),
    )
    return ddir.name, len(rows)


def cmd_grid(a):
    a.out.mkdir(parents=True, exist_ok=True)
    sett = settlements(a.cache)
    grid = candidates()
    (a.out / "grid.json").write_text(
        json.dumps(
            {
                "candidates": grid,
                "lat_us": LAT_US,
                "gap_us": GAP_US,
                "age_us": AGE_US,
                "size": SIZE,
                "trig_max_us": TRIG_MAX_US,
                "settlements": len(sett),
                "fill": "trade-through quote proxy",
                "maker_fee": 0,
            },
            indent=0,
        )
    )
    jobs = [(d, a.out, sett) for d in done_days(a.cache)]
    with Pool(a.workers) as pool:
        for d, k in pool.imap_unordered(_grid_day, jobs):
            print(d, k, flush=True)


def cmd_diag(a):
    """Invalid/stale stretch lengths and recorder gaps on a sample of cached days."""
    stretch, gaps, inval, ev = [], [], 0, 0
    for ddir in done_days(a.cache)[: a.days]:
        for _, m in markets(ddir):
            t = m["t"]
            if len(t) < 2:
                continue
            good = (m["bid"] >= 0).all(1) & ((t[:, None] - m["seen"]) <= AGE_US).all(1)
            ev += len(t)
            inval += int((~good).sum())
            d = np.diff(np.r_[0, (~good).astype(np.int8), 0])
            s0, s1 = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
            end = np.r_[t, MKT_US][s1]  # time of next good event (or market end)
            stretch += list((end - t[s0]) / 1e6)
            gaps += list(np.diff(t)[np.diff(t) > GAP_US] / 1e6)
    q = [50, 90, 99, 99.9]
    st = np.array(stretch) if stretch else np.zeros(1)
    print(
        json.dumps(
            {
                "events": ev,
                "bad_event_share": round(inval / max(ev, 1), 4),
                "bad_stretches": len(stretch),
                "bad_stretch_s_pct": dict(zip(map(str, q), np.percentile(st, q).round(3).tolist())),
                "bad_stretches_over_age": int((st > AGE_US / 1e6).sum()),
                "recorder_gaps_over_5s": len(gaps),
                "gap_s_max": max(gaps, default=0),
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------- walk-forward


def day_stats(z, use_obs=False):
    o = np.argsort(z["epochs"])  # markets in time order
    lo = z["lo"].astype(float)[o]
    cens = z["status"][o] >= CENS_PEND
    obs = np.where(cens, 0.0, lo)  # censored trades excluded (assumes censoring uninformative)
    x = obs if use_obs else lo
    eq = np.cumsum(x, 0)
    dd = (np.maximum.accumulate(np.vstack([np.zeros((1, x.shape[1])), eq]), 0)[1:] - eq).max(0)
    return {
        "pnl": eq[-1],
        "lo": lo.sum(0),
        "obs": obs.sum(0),
        "cens": cens.sum(0),
        "hi": z["hi"].astype(float).sum(0),
        "score": eq[-1] - 0.5 * dd,
        "n": z["entered"].sum(0),
        "markets": len(o),
        "status": z["status"],
    }


def choose(S, N, train, val, minimum, topk):
    tr = np.median(S[train], 0)
    va = np.median(S[val], 0)
    ok = (N[train].sum(0) >= minimum) & (N[val].sum(0) >= minimum)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return None
    top = idx[np.lexsort((idx, -tr[idx]))][:topk]
    top = top[np.lexsort((top, -tr[top], -va[top]))]
    return int(top[0]) if va[top[0]] > 0 else None


def neighbours(grid, c):
    pos = {k: GRID[k] for k in ("x", "b", "s", "ttl")}
    out = []
    for i, k in enumerate(pos):
        vals = pos[k]
        j = vals.index(c[i])
        for jj in (j - 1, j + 1):
            if 0 <= jj < len(vals):
                n = list(c)
                n[i] = vals[jj]
                if tuple(n) in grid:
                    out.append(grid.index(tuple(n)))
    return out


def cmd_wf(a):
    grid = [tuple(c) for c in json.loads((a.out / "grid.json").read_text())["candidates"]]
    days, st = [], []
    for f in sorted(a.out.glob("????-??-??.npz")):
        s = day_stats(np.load(f), a.score == "obs")
        if s["markets"] >= a.min_markets:
            days.append(f.stem)
            st.append(s)
    S = np.array([s["score"] for s in st])
    P = np.array([s["pnl"] for s in st])
    H = np.array([s["hi"] for s in st])
    L = np.array([s["lo"] for s in st])
    O = np.array([s["obs"] for s in st])
    C = np.array([s["cens"] for s in st])
    N = np.array([s["n"] for s in st])
    nd, w = len(days), a.train_days + a.validation_days
    if nd < w + a.holdout_days + 1:
        sys.exit(f"{nd} usable days; need > {w + a.holdout_days}")
    h0 = nd - a.holdout_days
    daily, picks = [], Counter()
    frozen = None
    for i in range(w, nd):
        if i <= h0:
            sel = choose(
                S,
                N,
                list(range(i - w, i - a.validation_days)),
                list(range(i - a.validation_days, i)),
                a.min_entries,
                a.top_k,
            )
            if i == h0:
                frozen = sel
        else:
            sel = frozen
        picks[sel] += 1
        c = None if sel is None else grid[sel]
        daily.append(
            {
                "day": days[i],
                "phase": "holdout" if i >= h0 else "dev_oos",
                "params": c,
                "lo": 0.0 if sel is None else L[i, sel],
                "obs": 0.0 if sel is None else O[i, sel],
                "hi": 0.0 if sel is None else H[i, sel],
                "entries": 0 if sel is None else int(N[i, sel]),
                "censored": 0 if sel is None else int(C[i, sel]),
                "prior_lo": L[i, grid.index(PRIOR)],
                "prior_obs": O[i, grid.index(PRIOR)],
                "prior_hi": H[i, grid.index(PRIOR)],
                "prior_n": int(N[i, grid.index(PRIOR)]),
                "prior_censored": int(C[i, grid.index(PRIOR)]),
            }
        )
    rng = np.random.default_rng(0)

    def summ(rows, key="lo", nkey="entries"):
        v = np.array([r[key] for r in rows])
        bs = [rng.choice(v, len(v)).mean() for _ in range(2000)] if len(v) else [0]
        return {
            "days": len(v),
            "total": float(v.sum()),
            "mean_day": float(v.mean()) if len(v) else 0,
            "ci90_mean_day": [float(np.percentile(bs, 5)), float(np.percentile(bs, 95))],
            "trades": int(sum(r[nkey] for r in rows)),
            "pnl_per_trade": float(v.sum() / max(1, sum(r[nkey] for r in rows))),
            "cash_days": sum(r.get("params", 1) is None for r in rows),
        }

    dev = [r for r in daily if r["phase"] == "dev_oos"]
    ho = [r for r in daily if r["phase"] == "holdout"]
    hold = list(range(h0, nd))
    summary = {
        "usable_days": days,
        "holdout_start": days[h0],
        "frozen": None if frozen is None else grid[frozen],
        "score_mode": a.score,
        "dev_oos": summ(dev),
        "dev_oos_obs": summ(dev, "obs"),
        "dev_oos_hi": summ(dev, "hi"),
        "holdout": summ(ho),
        "holdout_obs": summ(ho, "obs"),
        "holdout_hi": summ(ho, "hi"),
        "prior_fixed_dev": summ(dev, "prior_lo", "prior_n"),
        "prior_fixed_dev_obs": summ(dev, "prior_obs", "prior_n"),
        "prior_fixed_dev_hi": summ(dev, "prior_hi", "prior_n"),
        "prior_fixed_holdout": summ(ho, "prior_lo", "prior_n"),
        "prior_fixed_holdout_obs": summ(ho, "prior_obs", "prior_n"),
        "prior_fixed_holdout_hi": summ(ho, "prior_hi", "prior_n"),
        "selection_counts": {
            str(None if k is None else grid[k]): v for k, v in picks.most_common()
        },
        "holdout_grid_share_positive": float((L[hold].sum(0) > 0).mean()),
        "holdout_grid_share_positive_obs": float((O[hold].sum(0) > 0).mean()),
        "execution": "QUOTE_PROXY trade-through, latency 250ms, maker fee 0",
    }
    pi = grid.index(PRIOR)
    summary["prior_status_all_days"] = dict(
        Counter(STATUS[x] for s_ in st for x in s_["status"][:, pi])
    )
    allc = np.bincount(np.concatenate([s_["status"].ravel() for s_ in st]), minlength=8)
    summary["status_share_all_candidates"] = {
        STATUS[k]: round(float(v / allc.sum()), 4) for k, v in enumerate(allc)
    }
    if frozen is not None:
        summary["frozen_neighbours_holdout"] = {
            str(grid[k]): float(P[hold, k].sum()) for k in neighbours(grid, grid[frozen])
        }
        summary["frozen_holdout_status"] = dict(
            Counter(STATUS[x] for i in hold for x in st[i]["status"][:, frozen])
        )
    # descriptive only: dev-period table, never used for the frozen pick
    dv = list(range(h0))
    top = np.argsort(-P[dv].sum(0))[:25]
    with (a.out / "dev_in_sample_top25.csv").open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(
            [
                "x",
                "b",
                "s",
                "ttl",
                "dev_lo",
                "dev_obs",
                "dev_hi",
                "trades",
                "censored",
                "pos_day_share",
                "holdout_lo",
                "holdout_obs",
            ]
        )
        for k in top:
            wr.writerow(
                [
                    *grid[k],
                    round(L[dv, k].sum(), 3),
                    round(O[dv, k].sum(), 3),
                    round(H[dv, k].sum(), 3),
                    int(N[dv, k].sum()),
                    int(C[dv, k].sum()),
                    round(float((P[dv, k] > 0).mean()), 3),
                    round(L[hold, k].sum(), 3),
                    round(O[hold, k].sum(), 3),
                ]
            )
    with (a.out / "daily_oos.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(daily[0]))
        wr.writeheader()
        wr.writerows(daily)
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(
        json.dumps({k: v for k, v in summary.items() if k != "usable_days"}, indent=2, default=str)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prep")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("settle")
    p.add_argument("--cache", type=Path, required=True)
    p = sp.add_parser("diag")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--days", type=int, default=3)
    p = sp.add_parser("grid")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p = sp.add_parser("wf")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--train-days", type=int, default=14)
    p.add_argument("--validation-days", type=int, default=7)
    p.add_argument("--holdout-days", type=int, default=10)
    p.add_argument("--min-entries", type=int, default=30)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--min-markets", type=int, default=250)
    p.add_argument("--score", choices=("lo", "obs"), default="lo")
    a = ap.parse_args()
    {"prep": cmd_prep, "settle": cmd_settle, "diag": cmd_diag, "grid": cmd_grid, "wf": cmd_wf}[
        a.cmd
    ](a)


if __name__ == "__main__":
    main()
