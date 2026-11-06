"""Paired-token gap scan, BTC 5m up/down. One pass over the same_token_wf2 npz cache.

A) taker arb: runs of ask_U+ask_D<1 (raw) and net of taker fee, latency/size executability
B) bid-sum gap 1-(bid_U+bid_D) = two-sided maker room, time-weighted by time-in-market x |mid-50|
C) quote-proxy two-sided maker: join/improve both bids, TTL, unpaired leg -> settle or complete as taker
D) favourite flips (65/70) and settle-with-last-favourite
M) mirror check: ask_D == 1-bid_U and ask_U == 1-bid_D (CLOB complement matching)
"""

import argparse
import csv
import json
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from same_token_wf2 import AGE_US, GAP_US, LAT_US, MKT_US, SIZE, day_of, done_days, markets, settlements

FEE = 0.07  # crypto taker: C*0.07*p*(1-p); maker 0
TB = 60_000_000
MB = np.array([0, 100, 200, 300, 400])  # |mid-500| bucket edges, milli
GTH = (10, 20, 30, 50, 80)
CK = (5, 30, 60, 120, 180, 240)  # maker post time, s
TTL = (10, 30, 60)
IMP = (0, 10)  # improve each bid by ticks*milli
FILL = (0, 1)  # 0 trade-through (ask<px), 1 touch (ask<=px)
CFG = [(c, ttl, imp, f) for c in CK for ttl in TTL for imp in IMP for f in FILL]
BIG = 1 << 60
HOLDOUT = "2026-07-19"


def fee(p):
    return FEE * p * (1000 - p) / 1000  # milli per share


def first(mask, lo):
    k = np.flatnonzero(mask)
    return lo + int(k[0]) if len(k) else BIG


def scan(m, W):
    t = m["t"]
    n = len(t)
    if n < 2:
        return None
    bid, ask, asz, upd = m["bid"], m["ask"], m["asz"], m["upd"]
    gs = (bid >= 0) & ((t[:, None] - m["seen"]) <= AGE_US)
    good = gs.all(1)
    gapi = np.diff(t, prepend=0) > GAP_US
    w = np.diff(t, append=MKT_US).astype(float)
    w[w > GAP_US] = 0
    w *= good
    b32, a32 = bid.astype(np.int64), ask.astype(np.int64)

    # M) mirror
    dU = a32[:, 0] - (1000 - b32[:, 1])
    dD = a32[:, 1] - (1000 - b32[:, 0])
    mir = (dU == 0) & (dD == 0)

    # B) gap tables
    g = 1000 - b32.sum(1)
    tb = np.minimum(t // TB, 4)
    mb = np.searchsorted(MB, np.abs(b32[:, 0] + a32[:, 0] - 1000) / 2, "right") - 1
    cell = (tb * 5 + mb).clip(0, 24)
    H = np.zeros((2 + len(GTH), 25))
    H[0] = np.bincount(cell, w, 25)
    H[1] = np.bincount(cell, w * g, 25)
    for i, x in enumerate(GTH):
        H[2 + i] = np.bincount(cell, w * (g >= x), 25)

    # A) taker arb episodes on raw sum<1000
    s_ask = a32.sum(1)
    net = np.where(good, 1000 - s_ask - fee(a32[:, 0]) - fee(a32[:, 1]), -1e9)
    R = good & (s_ask < 1000) & ~gapi
    d = np.diff(np.r_[0, R.astype(np.int8), 0])
    st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    eps = []
    for s, e in zip(st, en):
        dur = (t[e] if e < n else MKT_US) - t[s]
        x = int(np.searchsorted(t, t[s] + LAT_US, "right")) - 1
        ok = good[x] and asz[x].min() >= SIZE
        eps.append(
            (
                int(t[s]),
                int(dur),
                int(1000 - s_ask[s:e].min()),
                float(net[s:e].max()),
                float(net[x]) if ok else np.nan,
                int(max(t[s] - m["seen"][s, 0], t[s] - m["seen"][s, 1])),
                float(asz[s].min()),
                int(upd[s, 0]) + 2 * int(upd[s, 1]),
            )
        )

    # C) two-sided maker
    gi = np.flatnonzero(good)
    gsi = [np.flatnonzero(gs[:, k]) for k in (0, 1)]
    mk = np.full((len(CFG), 4), np.nan)  # code, gap0, pnl_settle, pnl_complete (milli/share)
    for ci, (c, ttl, imp, fm) in enumerate(CFG):
        k0 = np.searchsorted(gi, np.searchsorted(t, c * 1_000_000))
        if k0 >= len(gi) or t[gi[k0]] - c * 1_000_000 > AGE_US:
            mk[ci, 0] = -1
            continue
        i0 = gi[k0]
        px = b32[i0] + imp
        px = np.where(px < a32[i0], px, b32[i0])
        g0 = 1000 - px.sum()
        mk[ci, 1] = 1000 - b32[i0].sum()
        if g0 < 10:
            mk[ci, 0] = -2
            continue
        t1 = t[i0] + LAT_US
        t2 = min(t1 + ttl * 1_000_000, MKT_US)
        lo, hi = int(np.searchsorted(t, t1)), int(np.searchsorted(t, t2, "right"))
        j = []
        for k in (0, 1):
            a_ = a32[lo:hi, k]
            hit = (a_ <= px[k]) if fm else (a_ < px[k])
            j.append(first(gs[lo:hi, k] & upd[lo:hi, k] & hit & (asz[lo:hi, k] >= SIZE), lo))
        if j[0] < BIG and j[1] < BIG:
            mk[ci, 0], mk[ci, 2], mk[ci, 3] = 1, g0, g0
        elif j[0] == BIG and j[1] == BIG:
            mk[ci, 0], mk[ci, 2], mk[ci, 3] = 0, 0, 0
        else:
            k = 0 if j[0] < BIG else 1
            o = 1 - k
            mk[ci, 0] = 2 + k
            ps = (1000.0 * (W == k) - px[k]) if W is not None else np.nan
            mk[ci, 2] = ps
            q = np.searchsorted(gsi[o], hi - 1) if t2 < MKT_US else len(gsi[o])
            if q < len(gsi[o]):
                pa = a32[gsi[o][q], o]
                mk[ci, 3] = 1000 - px[k] - pa - fee(pa)
            else:
                mk[ci, 3] = ps

    # D) flips
    midU = (b32[good, 0] + a32[good, 0]) / 2
    fl = []
    for x in (650, 700):
        sgn = np.where(midU >= x, 1, np.where(midU <= 1000 - x, -1, 0))
        nz = sgn[sgn != 0]
        fl += [int((nz[1:] != nz[:-1]).sum()) if len(nz) else -1, int(nz[-1]) if len(nz) else 0]

    row = dict(
        n=n,
        obs_s=w.sum() / 1e6,
        gbar=float((w * g).sum() / w.sum()) if w.sum() else np.nan,
        mirror=float((w * mir).sum() / w.sum()) if w.sum() else np.nan,
        mirU_neg=float((w * (dU < 0)).sum() / w.sum()) if w.sum() else np.nan,
        mirD_neg=float((w * (dD < 0)).sum() / w.sum()) if w.sum() else np.nan,
        raw_eps=len(eps),
        raw_time_s=float(sum(e[1] for e in eps)) / 1e6,
        net_eps=sum(e[3] > 0 for e in eps),
        exec_eps=sum(e[4] > 0 for e in eps if e[4] == e[4]),
        exec_pnl=sum(e[4] * SIZE / 1000 for e in eps if e[4] == e[4] and e[4] > 0),
        flips65=fl[0],
        last65=fl[1],
        flips70=fl[2],
        last70=fl[3],
    )
    return row, H, eps, mk


def _day(args):
    ddir, out, sett = args
    rows, Hs, eps, mks = [], np.zeros((2 + len(GTH), 25)), [], []
    for e, m in markets(ddir):
        W = sett.get(e)
        r = scan(m, W)
        if r is None:
            continue
        row, H, ep, mk = r
        row.update(epoch=e, day=ddir.name, winner=-1 if W is None else W)
        rows.append(row)
        Hs += H
        eps += [(e,) + x for x in ep]
        mks.append(mk)
    if rows:
        np.savez_compressed(
            out / f"{ddir.name}.npz",
            H=Hs,
            mk=np.stack(mks).astype(np.float32),
            epochs=np.array([r["epoch"] for r in rows]),
            eps=np.array(eps, float).reshape(-1, 9),
        )
        with (out / f"{ddir.name}.csv").open("w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    return ddir.name, len(rows)


def cmd_scan(a):
    a.out.mkdir(parents=True, exist_ok=True)
    sett = settlements(a.cache)
    (a.out / "cfg.json").write_text(
        json.dumps(dict(cfg=CFG, lat_us=LAT_US, age_us=AGE_US, size=SIZE, fee=FEE), indent=0)
    )
    days = done_days(a.cache)
    if a.days:
        days = days[: a.days]
    jobs = [(d, a.out, sett) for d in days if a.force or not (a.out / f"{d.name}.npz").exists()]
    with Pool(a.workers) as p:
        for d, k in p.imap_unordered(_day, jobs):
            print(d, k, flush=True)


def boot(daily, B=2000, seed=0):
    r = np.random.default_rng(seed)
    x = np.asarray(daily, float)
    if len(x) < 2:
        return np.nan, np.nan
    s = x[r.integers(0, len(x), (B, len(x)))].sum(1)
    return float(np.percentile(s, 5)), float(np.percentile(s, 95))


def pct(x, q):
    return float(np.percentile(x, q)) if len(x) else np.nan


def cmd_report(a):
    fs = sorted(a.out.glob("????-??-??.npz"))
    rows = []
    for f in fs:
        rows += list(csv.DictReader(f.with_suffix(".csv").open()))
    Z = [np.load(f) for f in fs]
    days = [f.stem for f in fs]
    H = sum(z["H"] for z in Z)
    E = np.concatenate([z["eps"] for z in Z])
    rep = {"days": len(days), "markets": len(rows), "first": days[0], "last": days[-1]}
    fl = lambda k: np.array([float(r[k]) for r in rows])  # noqa: E731
    rep["mirror_time_frac"] = float(np.nanmean(fl("mirror")))
    rep["mirror_U_cheaper_frac"] = float(np.nanmean(fl("mirU_neg")))
    rep["mirror_D_cheaper_frac"] = float(np.nanmean(fl("mirD_neg")))
    rep["mean_bid_gap_milli"] = float((H[1].sum()) / H[0].sum())
    for i, x in enumerate(GTH):
        rep[f"time_frac_gap_ge_{x}"] = float(H[2 + i].sum() / H[0].sum())
    # gap tables
    tw = H[0].reshape(5, 5)
    rep["gap_mean_by_minute_x_absmid"] = np.round(H[1].reshape(5, 5) / np.maximum(tw, 1), 1).tolist()
    rep["gap_ge20_by_minute_x_absmid"] = np.round(H[3].reshape(5, 5) / np.maximum(tw, 1), 3).tolist()
    rep["time_share_by_minute_x_absmid"] = np.round(tw / tw.sum(), 3).tolist()
    # taker arb: cols epoch,t,dur,raw_edge,net_max,net_exec,age_old,size0,updmask
    nm = len(rows)
    dur = E[:, 2] / 1000 if len(E) else np.zeros(0)
    ex = E[:, 5] if len(E) else np.zeros(0)
    exok = np.nan_to_num(ex, nan=-1) > 0
    rep["taker"] = dict(
        raw_eps=len(E),
        raw_eps_per_market=len(E) / nm,
        markets_with_raw=int(len(np.unique(E[:, 0]))) if len(E) else 0,
        raw_dur_ms_p50_p90_p99=[pct(dur, q) for q in (50, 90, 99)],
        raw_edge_milli_p50_p90_max=[pct(E[:, 3], 50), pct(E[:, 3], 90), pct(E[:, 3], 100)] if len(E) else [],
        frac_raw_lasting_gt_lat=float((E[:, 2] > LAT_US).mean()) if len(E) else np.nan,
        net_of_fee_eps=int((E[:, 4] > 0).sum()) if len(E) else 0,
        executable_eps=int(exok.sum()),
        executable_markets=int(len(np.unique(E[exok, 0]))) if exok.any() else 0,
        exec_pnl_usd_5sh=float((ex[exok] * SIZE / 1000).sum()),
        exec_onset_age_ms_p50=pct(E[exok, 6] / 1000, 50) if exok.any() else np.nan,
        exec_size0_p50=pct(E[exok, 7], 50) if exok.any() else np.nan,
    )
    dd = np.array([d for d in days])
    if exok.any():
        eday = np.array([day_of(e) for e in E[exok, 0].astype(int)])
        rep["taker"]["exec_pnl_by_day"] = {d: float((ex[exok][eday == d] * SIZE / 1000).sum()) for d in dd}
    # maker
    mk = [z["mk"] for z in Z]  # per day (markets, cfg, 4)
    hold = np.array([d >= HOLDOUT for d in days])
    out = []
    for ci, (c, ttl, imp, fm) in enumerate(CFG):
        r = dict(post_s=c, ttl=ttl, imp=imp, fill="touch" if fm else "through")
        allm = np.concatenate([m[:, ci] for m in mk])
        code = allm[:, 0]
        posted = code >= 0
        r["posted"] = int(posted.sum())
        r["both"] = float((code == 1).sum() / max(posted.sum(), 1))
        r["one"] = float((code >= 2).sum() / max(posted.sum(), 1))
        r["gap0_mean"] = float(np.nanmean(allm[posted, 1])) if posted.any() else np.nan
        for j, nmk in ((2, "settle"), (3, "complete")):
            dsum = np.array([np.nansum(m[:, ci, j]) * SIZE / 1000 for m in mk])
            for tag, sel in (("pre", ~hold), ("hold", hold)):
                x = dsum[sel]
                lo, hi = boot(x)
                r[f"{nmk}_{tag}_usd"] = round(float(x.sum()), 2)
                r[f"{nmk}_{tag}_ci90"] = [round(lo, 2), round(hi, 2)]
            # adverse selection on one-leg fills
            one = code >= 2
            r[f"{nmk}_one_leg_mean_milli"] = float(np.nanmean(allm[one, j])) if one.any() else np.nan
        out.append(r)
    rep["maker"] = out
    # maker by gap at post: through fill, no improve, ttl 30, pooled by post time
    gb = (10, 20, 30, 50, 80, 1001)
    tab = {}
    for c in CK:
        cj = CFG.index((c, 30, 0, 0))
        allm = np.concatenate([m[:, cj] for m in mk])
        for lo_, hi_ in zip(gb[:-1], gb[1:]):
            sel = (allm[:, 0] >= 0) & (allm[:, 1] >= lo_) & (allm[:, 1] < hi_)
            if sel.sum():
                tab[f"t{c}_gap{lo_}-{hi_ - 1}"] = dict(
                    n=int(sel.sum()),
                    both=round(float((allm[sel, 0] == 1).mean()), 3),
                    one=round(float((allm[sel, 0] >= 2).mean()), 3),
                    complete_mean_milli=round(float(np.nanmean(allm[sel, 3])), 2),
                    settle_mean_milli=round(float(np.nanmean(allm[sel, 2])), 2),
                )
    rep["maker_by_gap0"] = tab
    # flips
    for x in ("65", "70"):
        f_ = fl(f"flips{x}")
        lst = fl(f"last{x}")
        win = fl("winner")
        ok = (f_ >= 0) & (win >= 0)
        lastside = np.where(lst == 1, 0, 1)
        rep[f"flips{x}"] = dict(
            markets=int(ok.sum()),
            mean=float(f_[ok].mean()),
            hist={int(k): int(v) for k, v in zip(*np.unique(np.minimum(f_[ok], 6), return_counts=True))},
            p_settle_last_fav=float((lastside[ok] == win[ok]).mean()),
        )
    (a.out / "report.json").write_text(json.dumps(rep, indent=1, default=float))
    T = rep["taker"]
    print(f"{rep['markets']} markets / {rep['days']} days {rep['first']}..{rep['last']}")
    print(f"mirror exact {rep['mirror_time_frac']:.4f} of time; mean bid gap {rep['mean_bid_gap_milli']:.1f} milli")
    print("time frac gap>=", {x: round(rep[f'time_frac_gap_ge_{x}'], 3) for x in GTH})
    print(
        f"taker raw eps {T['raw_eps']} ({T['raw_eps_per_market']:.2f}/mkt) dur ms p50/90/99 "
        f"{np.round(T['raw_dur_ms_p50_p90_p99'], 1)} >lat {T['frac_raw_lasting_gt_lat']:.3f} "
        f"net {T['net_of_fee_eps']} exec {T['executable_eps']} in {T['executable_markets']} mkts "
        f"pnl ${T['exec_pnl_usd_5sh']:.2f}"
    )
    print("gap mean by minute (rows) x |mid-50| 0-10,10-20,20-30,30-40,40-50c (cols):")
    for rr in rep["gap_mean_by_minute_x_absmid"]:
        print("  ", rr)
    print("maker (sorted by complete_pre):")
    for r in sorted(out, key=lambda r: -r["complete_pre_usd"])[: a.top]:
        print(
            f"  t{r['post_s']:>3} ttl{r['ttl']:>2} imp{r['imp']:>2} {r['fill']:7} posted {r['posted']:>6} "
            f"both {r['both']:.3f} one {r['one']:.3f} | settle pre {r['settle_pre_usd']:>8} hold {r['settle_hold_usd']:>7} "
            f"| complete pre {r['complete_pre_usd']:>8} {r['complete_pre_ci90']} hold {r['complete_hold_usd']:>7} "
            f"| 1-leg {r['complete_one_leg_mean_milli']:.1f}m"
        )
    print("maker by gap at post (through, imp0, ttl30), mean pnl milli/share per post:")
    for k, v in tab.items():
        print(f"  {k:>16} {v}")
    for x in ("65", "70"):
        print(f"flips{x}", rep[f"flips{x}"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("scan")
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    {"scan": cmd_scan, "report": cmd_report}[a.cmd](a)
