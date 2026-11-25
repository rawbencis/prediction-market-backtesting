"""LT1 validation V1/V2 on synthetic books (frozen/LT1_plan.json "validation").

Synthetic BTC-5m-like window: fair value v (UP, 1e-4 $, on half cents so bid = v - 50 and ask = v + 50 are whole cents)
moves +-1c on a 100 ms grid; displayed sizes 40-160; taker prints at the bid / ask at random (independent of the future,
so v is a martingale and the prints carry no information). Settlement pays the expected value v_end (removes coin-flip
noise; still martingale-consistent).
  V1 martingale: P(move) = 0.04 per step, up/down 50/50.
  V2 planted mean reversion: after a move the next move reverses it with prob 0.8 (a drop of UP or of DOWN bounces;
     the first version reverted only UP drops, which made UP drift up all day and DOWN ladders lose, see report).
Identity checked in V1 (per fill event, $): pnl = fsz*(mid_1s - p) + sum_tp(tp - mid before the TP fill) + martingale terms.
Usage: python lt1_validate.py v1|v2 [n_windows]
"""
import sys

import numpy as np

import lt1_sim as L

TF = 5


def synth(rng, e, p_move, rev_after_drop):
    t0 = e * 1000
    t = np.arange(t0 - 5000, t0 + TF * 60000, 100, dtype=np.int64)
    v = np.empty(len(t), np.int64)
    v[0] = 4950 + 100 * int(rng.integers(-5, 6))
    last = 0
    u, w = rng.random(len(t)), rng.random(len(t))
    for i in range(1, len(t)):
        mv = 0
        if u[i] < p_move:
            pu = 0.5 if last == 0 else (rev_after_drop if last < 0 else 1 - rev_after_drop)  # reversion after a drop of either token
            mv = 100 if w[i] < pu else -100
            last = mv
        v[i] = min(max(v[i - 1] + mv, 150), 9850)
    bid, ask = v - 50, v + 50
    bs = np.round(rng.uniform(40, 160, len(t)), 2)
    ks = np.round(rng.uniform(40, 160, len(t)), 2)
    # sizes refresh only every ~1 s or on a move
    keep = (rng.random(len(t)) > 0.1) & np.r_[False, v[1:] == v[:-1]]
    for i in np.flatnonzero(keep):
        bs[i], ks[i] = bs[i - 1], ks[i - 1]
    # prints: taker sell at bid / buy at ask; deplete the displayed size (book row at the same ms = post-trade state)
    ps = rng.random(len(t))
    px, phb, pup, psz = [], [], [], []
    for i in range(len(t)):
        if ps[i] < 0.15:
            n = float(np.round(rng.uniform(1, 30), 2))
            px.append(t[i]); phb.append(True); pup.append(bid[i]); psz.append(n)
            bs[i] = max(bs[i] - n, 1.0)
        elif ps[i] < 0.30:
            n = float(np.round(rng.uniform(1, 30), 2))
            px.append(t[i]); phb.append(False); pup.append(ask[i]); psz.append(n)
            ks[i] = max(ks[i] - n, 1.0)
    book = dict(x=t, b=bid.astype(np.uint16), k=ask.astype(np.uint16), bs=bs.astype(np.float32), ks=ks.astype(np.float32))
    P = dict(x=np.array(px, np.int64), x_print=np.array(px, np.int64), matched=np.ones(len(px), bool), sz=np.array(psz),
             hb=np.array(phb, bool), up=np.array(pup, np.int64), tx=None)
    return dict(book=book, pr=P), float(v[-1]) / 1e4


def run(kind, n):
    rng = np.random.default_rng(20261126 if kind == "v1" else 20261127)
    p_move, rev = (0.04, 0.5) if kind == "v1" else (0.04, 0.8)
    L.PULLS = {"none": None}  # no Binance on synthetic books: pulls would equal 'none'
    empty = {k: {0: np.zeros(0, np.int64), 1: np.zeros(0, np.int64)} for k in L.PULLS if L.PULLS[k] is not None}
    rows, rungs, caps = [], [], []
    e0 = 1900000000
    for w in range(n):
        e = e0 + 300 * w
        m, pay_up = synth(rng, e, p_move, rev)
        k0 = len(rows)
        L.run_window(m, TF, e, pay_up, empty, rows, rungs, models={"pess": 0})
        A = L.Arr(m)
        ci = {c: j for j, c in enumerate(L.COLS)}
        R = np.array(rows[k0:], dtype=object)
        if len(R):
            tok = R[:, ci["tok"]].astype(int)
            c = np.zeros((len(R), 2 * len(L.XS)))
            for n, x in enumerate(L.XS):
                tt = R[:, ci[f"tpt{x}"]].astype(np.int64)
                tp = R[:, ci[f"tp{x}"]].astype(float)
                s1 = R[:, ci[f"tps1_{x}"]].astype(float)
                for tk in (0, 1):
                    sel = (tok == tk) & (s1 > 0)
                    if sel.any():
                        c[sel, n] = s1[sel] * (tp[sel] - A.mids(tt[sel], tk)) / 1e4  # mid in force at the TP fill (after it)
                        c[sel, len(L.XS) + n] = s1[sel] * (tp[sel] - A.mids(tt[sel] - 1, tk)) / 1e4  # mid just before
            caps.extend(c.tolist())
        if w % 500 == 499:
            print(kind, w + 1, len(rows), flush=True)
    import pandas as pd

    d = pd.DataFrame(rows, columns=L.COLS)
    d[[f"cap{x}" for x in L.XS] + [f"capb{x}" for x in L.XS]] = np.array(caps)
    d = d[d.pull == "none"]
    out = []
    for (step, off), g in d.groupby(["step", "off"]):
        for K in (1, 3, 5):
            s = g[g.rung < K]
            sh = s.fsz.sum()
            mo1 = 100 * (s.fsz * s.mo1 / 100).sum() / sh
            for x in L.XS:
                ev = 100 * s[f"pnl1_{x}"].sum() / sh
                cap = 100 * s[f"cap{x}"].sum() / sh
                capb = 100 * s[f"capb{x}"].sum() / sh
                hit = s[f"tps1_{x}"].sum() / sh
                # window-clustered SE of the residual
                res = (s[f"pnl1_{x}"] - s.fsz * s.mo1 / 100 - s[f"cap{x}"]).groupby(s.e).sum()
                shw = s.fsz.groupby(s.e).sum()
                bt = []
                rr = np.random.default_rng(0)
                ids = np.arange(len(res))
                for _ in range(500):
                    j = rr.integers(0, len(ids), len(ids))
                    bt.append(100 * res.values[j].sum() / shw.values[j].sum())
                lo, hi = np.percentile(bt, [5, 95])
                out.append((step, off, K, x, len(s), round(mo1, 3), round(ev, 3), round(ev - mo1, 3), round(capb, 3), round(cap, 3), round(ev - mo1 - cap, 3),
                            round(lo, 3), round(hi, 3), round(hit, 3)))
    o = pd.DataFrame(out, columns=["step", "off", "K", "x", "fills", "mo1_c", "EV_c", "EV_minus_mo1", "cap_before_c", "cap_at_fill_c", "resid_c", "resid_lo90", "resid_hi90", "tp_hit"])
    return d, o


if __name__ == "__main__":
    kind = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    d, o = run(kind, n)
    import pandas as pd

    pd.set_option("display.width", 200)
    print(o.to_string(index=False))
    L.OUT.mkdir(parents=True, exist_ok=True)
    o.to_csv(L.OUT / f"{kind}.csv", index=False)
    if kind == "v2":
        g = d[(d.pull == "none")]
        for x in L.XS:
          for tk, gg in [("both", g), ("UP", g[g.tok == 0]), ("DOWN", g[g.tok == 1])]:
            per_w = gg.groupby("e")[f"pnl1_{x}"].sum()
            shw = gg.groupby("e").fsz.sum()
            rr = np.random.default_rng(0)
            bt = [100 * per_w.values[j].sum() / shw.values[j].sum() for j in (rr.integers(0, len(per_w), len(per_w)) for _ in range(1000))]
            print(f"V2 {tk} ladders x={x}: EV {100 * per_w.sum() / shw.sum():+.3f}c/share 90% CI [{np.percentile(bt, 5):+.3f}, {np.percentile(bt, 95):+.3f}] mo1 {(gg.fsz * gg.mo1).sum() / gg.fsz.sum():+.3f}")
        for x in ():
            per_w = g.groupby("e")[f"pnl1_{x}"].sum()
            shw = g.groupby("e").fsz.sum()
            rr = np.random.default_rng(0)
            bt = [100 * per_w.values[j].sum() / shw.values[j].sum() for j in (rr.integers(0, len(per_w), len(per_w)) for _ in range(1000))]
            print(f"V2 all ladders x={x}: EV {100 * per_w.sum() / shw.sum():+.3f}c/share 90% CI [{np.percentile(bt, 5):+.3f}, {np.percentile(bt, 95):+.3f}]")
