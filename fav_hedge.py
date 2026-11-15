"""fav-hedge on dsim: leg 1 buys the favourite (or underdog: mirror), leg 2 rests a post-only bid on the other side at
q = 1 - p1 - fee1 - o; a naked-leg rule handles an unhedged leg 1. Grid in ~/dublin-retest/fav-hedge/grid.json.
Usage: python fav_hedge.py stage1|stage2|stage3 --days 2026-11-14 [--asset btc --tf 5]
"""
import argparse, json, time
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d

OUTD = Path.home() / "dublin-retest" / "fav-hedge"
SIZE, TICK, END_GUARD = 5.0, 100, 30_000

# ---------------------------------------------------------------- market helpers


def quotes(B, i, tok):
    """-> (bid, bid size, ask, ask size) of token tok in 1e-4, 0 when absent; None if the book is unknown."""
    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):
        return None
    b, k, bs, ks = int(B["b"][i]), int(B["k"][i]), float(B["bs"][i]), float(B["ks"][i])
    if tok == 0:
        return b, bs, k, ks
    return (10000 - k if k else 0), ks, (10000 - b if b else 0), bs


def seen(m, t_dec, sc):
    """Book row the strategy has seen at local decision time t_dec (exchange events up to t_dec - book delay)."""
    return d.state(m, t_dec - sc["book"], strict=False)


def bn_moves(bn, ai, bp, ms):
    """Binance spot trade times (ms) where price moved >= bp over the previous ms: (up_times, down_times)."""
    b = bn[(bn["src"] == 0) & (bn["a"] == ai)]
    b = b[np.argsort(b["T"], kind="stable")]
    T, p = b["T"].astype(np.int64), b["p"]
    r = (p / p[np.searchsorted(T, T - ms, "left")] - 1) * 1e4
    return T[r >= bp], T[r <= -bp]


def against(mv, tok):
    """Binance moves against holding `tok` (UP hurt by down moves)."""
    return mv[1] if tok == 0 else mv[0]


# ---------------------------------------------------------------- legs


def trigger(m, E, W, tr, sc):
    """-> (t_dec, favourite token) or None."""
    B, X = m["book"], int(round(tr["X"] * 1e4))
    if tr["kind"] == "fixed":
        td = (E + tr["t"]) * 1000
        q = quotes(B, seen(m, td, sc), 0)
        if not q or not q[0] or not q[2]:
            return None
        mid = (q[0] + q[2]) / 2
        return (td, 0) if mid >= X else (td, 1) if 10000 - mid >= X else None
    lo, hi = np.searchsorted(B["x"], E * 1000, "left"), np.searchsorted(B["x"], (E + W) * 1000 - END_GUARD - sc["book"], "left")
    if hi - lo < 2:
        return None
    b, k = B["b"][lo:hi].astype(int), B["k"][lo:hi].astype(int)
    au = np.where(k > 0, k, -1)
    ad = np.where(b > 0, 10000 - b, -1)
    cu = (au[1:] >= X) & (au[:-1] < X) & (au[:-1] >= 0)
    cd = (ad[1:] >= X) & (ad[:-1] < X) & (ad[:-1] >= 0)
    c = np.flatnonzero(cu | cd)
    if not len(c):
        return None
    j = c[0] + 1
    return int(B["x"][lo + j]) + sc["book"], (0 if cu[c[0]] else 1)


def leg1(m, td, tok, how, sc, model):
    """-> (fills list, t_last_fill) or None."""
    q = quotes(m["book"], seen(m, td, sc), tok)
    if not q:
        return None
    if how == "taker":
        if not q[2]:
            return None
        f = d.taker(m, td + sc["order"] + sc["hold"], tok, q[2] + 100, SIZE)
        return ([f], f["x"]) if f else None
    if not q[0]:
        return None
    o = d.maker(m, td + sc["order"], tok, q[0], SIZE, td + 5000 + sc["cancel"], model)
    fs = o["fills"]
    return (fs, fs[-1]["x"]) if fs else None


def leg2_orders(m, t0, tok2, qmax, mgmt, sc, end_dec, mv_pull):
    """Order segments [(price, t_arrive, t_cancel_arrive)] for leg 2 from decision time t0 until end_dec."""
    B = m["book"]

    def px_at(t, touch):
        q = quotes(B, seen(m, t, sc), tok2)
        p = qmax
        if q and touch and q[0]:
            p = min(p, q[0])
        if q and q[2]:
            p = min(p, q[2] - TICK)  # post-only: stay below the ask
        return p if p >= TICK else None

    segs = []
    if mgmt == "static":
        p = px_at(t0, False)
        if p:
            segs.append((p, t0 + sc["order"], end_dec + sc["cancel"]))
    elif mgmt == "reprice":
        cur, t_cur = None, None
        for t in range(int(t0), int(end_dec), 1000):
            p = px_at(t, True)
            if p != cur:
                if cur:
                    segs.append((cur, t_cur, t + sc["cancel"]))
                cur, t_cur = p, t + sc["order"]
        if cur:
            segs.append((cur, t_cur, end_dec + sc["cancel"]))
    else:  # pull while Binance moves against leg 1 (seen at T + bn), re-post after 1 s quiet
        ev = mv_pull + sc["bn"]
        ev = ev[(ev > t0 - 1000) & (ev < end_dec)]
        t = t0
        while t < end_dec:
            k = np.searchsorted(ev, t, "left")
            if k > 0 and t - ev[k - 1] < 1000:  # still inside a pulled spell: wait for 1 s of quiet
                t = ev[k - 1] + 1000
                continue
            nxt = ev[k] if k < len(ev) else end_dec
            p = px_at(t, False)
            if p:
                segs.append((p, t + sc["order"], min(nxt, end_dec) + sc["cancel"]))
            t = nxt + 1000 if k < len(ev) else end_dec
            if k < len(ev):  # extend quiet requirement through clusters
                while True:
                    k2 = np.searchsorted(ev, t, "left")
                    if k2 > 0 and t - ev[k2 - 1] < 1000:
                        t = ev[k2 - 1] + 1000
                    else:
                        break
    return segs


def run_leg2(m, segs, tok2, need, model, t_stop=None):
    """Simulate leg-2 segments in order; stop filling when `need` shares are filled. Cancel arrivals are capped at t_stop."""
    fills = []
    for p, ta, tc in segs:
        if t_stop is not None:
            tc = min(tc, t_stop)
        if ta >= tc:
            continue
        o = d.maker(m, ta, tok2, int(p), need - sum(f["size"] for f in fills), tc, model)
        fills += o["fills"]
        if sum(f["size"] for f in fills) >= need - 1e-9:
            break
    return fills


# ---------------------------------------------------------------- one entry


def simulate(m, E, W, winner, cfg, sc, model, mv):
    tr = trigger(m, E, W, cfg["trigger"], sc)
    if tr is None:
        return None
    td, fav = tr
    tok1 = fav if cfg["variant"] == "fav" else 1 - fav
    tok2 = 1 - tok1
    end = (E + W) * 1000
    if td > end - END_GUARD:
        return None
    l1 = leg1(m, td, tok1, cfg["leg1"], sc, model)
    if not l1:
        return None
    f1, t1 = l1
    s1 = sum(f["size"] for f in f1)
    p1 = sum(f["px"] * f["size"] for f in f1) / s1
    fee1 = sum(f["fee"] for f in f1)
    qmax = int((10000 - p1 - fee1 / s1 * 1e4 - cfg["o"] * 100) // TICK * TICK)
    t2 = t1 + sc["book"]  # we see our fill
    nk = cfg["naked"]
    # exit decision time if leg 2 still unfilled then
    B = m["book"]
    if nk["rule"] == "cut":
        t_exit = end - nk["c"] * 1000
    elif nk["rule"] == "stop":
        lo = np.searchsorted(B["x"], t2 - sc["book"], "left")
        hi = np.searchsorted(B["x"], end, "left")
        bid = B["b"][lo:hi].astype(int) if tok1 == 0 else np.where(B["k"][lo:hi] > 0, 10000 - B["k"][lo:hi].astype(int), 0)
        hit = np.flatnonzero((bid > 0) & (bid <= p1 - nk["d"] * 100))
        t_exit = int(B["x"][lo + hit[0]]) + sc["book"] if len(hit) else None
    elif nk["rule"] == "bnstop":
        ev = against(mv["stop"], tok1) + sc["bn"]
        k = np.searchsorted(ev, t2, "left")
        t_exit = int(ev[k]) if k < len(ev) and ev[k] < end else None
    else:
        t_exit = None
    if t_exit is not None and t_exit >= end:
        t_exit = None
    segs = leg2_orders(m, t2, tok2, qmax, cfg["leg2"], sc, end if t_exit is None else max(t_exit, t2), against(mv["pull"], tok1)) if qmax >= TICK else []
    f2 = run_leg2(m, segs, tok2, s1, model, None if t_exit is None else t_exit + sc["cancel"])
    h_seen = sum(f["size"] for f in f2 if t_exit is None or f["x"] + sc["book"] <= t_exit)
    sells = []
    if t_exit is not None and h_seen < s1 - 1e-9:
        need = s1 - h_seen
        for t in range(int(t_exit), int(end), 1000):  # FAK sell, retried every 1 s until it fills
            q = quotes(B, seen(m, t, sc), tok1)
            if q and q[0]:
                f = d.taker(m, t + sc["order"] + sc["hold"], tok1, max(TICK, q[0] - 100), need, min(5.0, need), sell=True)
                if f:
                    sells.append(f)
                    break
    # accounting
    pay = {0: 1.0 if winner == "UP" else 0.0, 1: 1.0 if winner == "DOWN" else 0.0}
    h2 = sum(f["size"] for f in f2)
    sold = sum(f["size"] for f in sells)
    c1 = sum(f["px"] / 1e4 * f["size"] + f["fee"] for f in f1)
    c2 = sum(f["px"] / 1e4 * f["size"] for f in f2)
    proceeds = sum(f["px"] / 1e4 * f["size"] - f["fee"] for f in sells)
    pnl1 = proceeds + (s1 - sold) * pay[tok1] - c1
    pnl2 = h2 * pay[tok2] - c2
    hedged = h2 >= s1 - 1e-9 and (t_exit is None or max(f["x"] for f in f2) + sc["book"] <= t_exit)
    return dict(E=E, tok1=tok1, p1=p1, s1=s1, q=qmax, h2=h2, sold=sold, hedged=int(hedged),
                t_hedge=(max(f["x"] for f in f2) - t1) if h2 >= s1 - 1e-9 else np.nan,
                exited=int(bool(sells)), pnl1=pnl1, pnl2=pnl2, pnl=pnl1 + pnl2, cost=c1 + c2,
                rebate=sum(f["rebate"] for f in f1 + f2))


# ---------------------------------------------------------------- runner


def load(days, asset, tf):
    out = []
    W = d.winners()
    for day in days:
        mk, pm, bn = d.load_day(day, asset=asset, tf=tf)
        ai = d.ASSETS.index(asset)
        mv = dict(pull=bn_moves(bn, ai, 2, 250), stop=bn_moves(bn, ai, 3, 500))
        pm_all = d.read(d.REC, day, "pm", d.PM_DT, lambda x: np.arange(len(x)) % 10 == 0)  # whole-day delay, 1/10 sample
        bn_all = d.read(d.REC, day, "bn", d.BN_DT, lambda x: (np.arange(len(x)) % 10 == 0) & (x["src"] == 0))
        sc = {k: d.scenario(k, pm_all, bn_all) for k in ("dublin", "tokyo", "colo")}
        del pm_all, bn_all
        for (a, t, e), m in sorted(mk.items()):
            w = W.get(d.slug((a, t, e)))
            if a == asset and t == tf and w:
                out.append((day, e, m, w, mv))
        del pm
    return out, sc


def evaluate(wins, cfgs, scn, model, sc):
    rows = []
    for ci, cfg in enumerate(cfgs):
        for day, E, m, w, mv in wins:
            r = simulate(m, E, cfg_W[0], w, cfg, sc[scn], model, mv)
            if r:
                rows.append(dict(cfg=ci, day=day, scn=scn, model=model, **r))
    return pd.DataFrame(rows)


cfg_W = [300]


def stats(R, n_days, B=2000, seed=1):
    """Per-config summary with a market (window) bootstrap of total PnL."""
    rng = np.random.default_rng(seed)
    out = []
    for c, g in R.groupby("cfg"):
        pn = g.pnl.values
        tot = pn.sum()
        boot = pn[rng.integers(0, len(pn), (B, len(pn)))].sum(1) if len(pn) > 1 else np.array([tot])
        lo, hi = np.percentile(boot, [5, 95])
        H = g[g.hedged == 1]
        N = g[g.hedged == 0]
        out.append(dict(cfg=c, entries=len(g), hedge_rate=round(len(H) / len(g), 3), t_hedge_p50_s=round(np.nanmedian(g.t_hedge) / 1000, 1) if g.t_hedge.notna().any() else np.nan,
                        locked_pnl=round(H.pnl.sum(), 2), naked_pnl=round(N.pnl.sum(), 2), naked_exit_share=round(N.exited.mean(), 2) if len(N) else np.nan,
                        leg1_pnl=round(g.pnl1.sum(), 2), leg2_pnl=round(g.pnl2.sum(), 2), pnl=round(tot, 2),
                        net_c_per_usd=round(100 * tot / g.cost.sum(), 2), usd_day=round(tot / n_days, 2),
                        usd_day_lo90=round(lo / n_days, 2), usd_day_hi90=round(hi / n_days, 2), rebate_est=round(g.rebate.sum(), 2)))
    return pd.DataFrame(out)


def family(cfg):
    return f'{cfg["trigger"]["kind"]}|{cfg["leg2"]}|{cfg["naked"]["rule"]}{cfg["naked"].get("c", cfg["naked"].get("d", ""))}'


def label(cfg):
    t = cfg["trigger"]
    tr = f'fixed t={t["t"]} X={t["X"]}' if t["kind"] == "fixed" else f'cross X={t["X"]}'
    nk = cfg["naked"]
    return f'{cfg["variant"]} | {tr} | leg1 {cfg["leg1"]} | o={cfg["o"]}c | leg2 {cfg["leg2"]} | naked {nk["rule"]}{nk.get("c", nk.get("d", ""))}'


def main(a):
    G = json.load(open(OUTD / "grid.json"))
    cfg_W[0] = a.tf * 60
    t0 = time.time()
    wins, sc = load(a.days, a.asset, a.tf)
    print(f"{len(wins)} windows {a.asset} {a.tf}m days {a.days}; scenarios {json.dumps(sc)}; load {time.time() - t0:.0f}s", flush=True)
    tag = f"{a.asset}{a.tf}m"
    if a.stage in ("stage1", "stage2"):
        cfgs = G[a.stage]["configs"][: a.limit or None]
        R = evaluate(wins, cfgs, "dublin", "pess", sc)
        R.to_csv(OUTD / f"{a.stage}-{tag}-entries.csv.gz", index=False)
        S = stats(R, len(a.days))
        S["label"] = [label(cfgs[c]) for c in S.cfg]
        S["family"] = [family(cfgs[c]) for c in S.cfg]
        S.to_csv(OUTD / f"{a.stage}-{tag}-summary.csv", index=False)
        print(f"done {len(cfgs)} configs in {time.time() - t0:.0f}s", flush=True)
    elif a.stage == "stage3":
        fz = json.load(open(OUTD / "frozen.json"))
        cfgs = [x["config"] for x in fz["pick_and_runners_up"]] + [x["config"] for x in fz["family_best"]]
        cells = [(s, q) for s in ("dublin", "tokyo", "colo") for q in ("pess", "prop", "front")] if a.all_cells else [("dublin", "pess")]
        allS = []
        for s, q in cells:
            R = evaluate(wins, cfgs, s, q, sc)
            S = stats(R, len(a.days))
            S["scn"], S["model"] = s, q
            allS.append(S)
            print(f"cell {s}/{q} done {time.time() - t0:.0f}s", flush=True)
        S = pd.concat(allS)
        S["label"] = [label(cfgs[c]) for c in S.cfg]
        S["family"] = [family(cfgs[c]) for c in S.cfg]
        S.to_csv(OUTD / f"{a.out_prefix or 'stage3'}-{tag}-summary.csv", index=False)


def freeze():
    """Apply the pre-set pick rule from grid.json to stage-1 dev results -> frozen.json (never overwritten)."""
    G = json.load(open(OUTD / "grid.json"))
    fz = OUTD / "frozen.json"
    if fz.exists():
        raise SystemExit("frozen.json exists: not re-freezing")
    S = pd.read_csv(OUTD / "stage1-btc5m-summary.csv")
    cfgs = G["stage1"]["configs"]
    ok = S[S.entries >= 40].sort_values(["usd_day_lo90", "entries"], ascending=[False, False])
    top = ok.head(3)
    fam = S.sort_values(["usd_day_lo90", "entries"], ascending=[False, False]).groupby("family").head(1)
    fam = fam[~fam.cfg.isin(top.cfg)]
    out = dict(frozen_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), dev_days=["2026-11-14"],
               print_shift_ms=d.PRINT_SHIFT_MS, queue_model_judged="pess", scenario_judged="dublin",
               rule=G["pick_rule"],
               pick_and_runners_up=[dict(cfg=int(r.cfg), label=r.label, dev=dict(entries=int(r.entries), usd_day=r.usd_day,
                                         usd_day_lo90=r.usd_day_lo90, net_c_per_usd=r.net_c_per_usd), config=cfgs[int(r.cfg)])
                                    for r in top.itertuples()],
               family_best=[dict(cfg=int(r.cfg), family=r.family, label=r.label, config=cfgs[int(r.cfg)]) for r in fam.itertuples()],
               holdout_command="cd ~/dublin-retest/repo && nice -n 19 ~/shadow-venv/bin/python fav_hedge.py stage3 --all-cells "
                               "--out-prefix holdout --days " + " ".join(f"2026-11-{d}" for d in range(21, 28)),
               pass_rule="dev pick under dublin+pess: holdout net c/$ with market-bootstrap 90% CI lower bound > 0, >= $3/day at"
                         " 5 shares, >= 300 entries, still >= 0 under prop, and clearly worse under tokyo")
    json.dump(out, open(fz, "w"), indent=1)
    print(json.dumps(out["pick_and_runners_up"], indent=1)[:3000])


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["freeze"]:
        freeze()
        raise SystemExit
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("stage1", "stage2", "stage3"))
    ap.add_argument("--days", nargs="+", default=["2026-11-14"])
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--tf", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all-cells", action="store_true")
    ap.add_argument("--out-prefix", default="")
    main(ap.parse_args())
