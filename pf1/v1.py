"""PF1 V1: synthetic window with a planted Polymarket lag. Lighter Up stays at 0.49/0.51 (100 shares). Every 30 s the
Polymarket Up ask drops from 0.50 to 0.42 (50 shares) for CORRECT_MS, then returns. Side b (sell Up on Lighter at 0.49,
buy Up on Polymarket at 0.42) clears > 4c after fees while it lasts.
Expect (amended latencies: Polymarket match = arrival + 320 ms): Polymarket-first from Dublin profits when the lag
lasts 500 ms; the same order of legs from Tokyo does not (sees it 112 ms late and is ~110 ms further from London);
Lighter-first from Tokyo (the probe) does not; nothing fills from Dublin if the lag lasts only 250 ms.
Usage: python -I v1.py  (exit 1 if an expectation fails)"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from engine import Side  # noqa: E402
from sim import Lat, ProbeLat, Win, pf1_attempt, probe_replay, triggers  # noqa: E402

E = 1795000000 - 1795000000 % 900


def synth(correct_ms):
    t0 = E * 1000.0
    # Lighter: a row every second
    lt = np.arange(t0, t0 + 900e3, 1000.0)
    n = len(lt)
    L = Side(lt, np.c_[np.full(n, 0.49), np.full((n, 2), np.nan)], np.c_[np.full(n, 100.0), np.zeros((n, 2))],
             np.c_[np.full(n, 0.51), np.full((n, 2), np.nan)], np.c_[np.full(n, 100.0), np.zeros((n, 2))], view=lt + 2)
    rows = [(t, 0.40, 0.50, 100.0) for t in np.arange(t0, t0 + 900e3, 1000.0) + 500]
    for k in np.arange(t0 + 10e3, t0 + 890e3, 30e3):
        rows += [(k, 0.40, 0.42, 50.0), (k + correct_ms, 0.40, 0.50, 100.0)]
    rows.sort()
    x = np.array([r[0] for r in rows])
    m = len(rows)
    bp = np.c_[[r[1] for r in rows], np.full((m, 2), np.nan)]
    ap = np.c_[[r[2] for r in rows], np.full((m, 2), np.nan)]
    as_ = np.c_[[r[3] for r in rows], np.zeros((m, 2))]
    bs = np.c_[np.full(m, 100.0), np.zeros((m, 2))]
    P = Side(x, bp, bs, ap, as_, view=x + 8)
    from engine import Trades
    P.trades = Trades(np.zeros((0, 7)))
    return Win(E, 0.05, P, L, True)


def run(correct_ms):
    out = {}
    w = synth(correct_ms)
    tr = triggers(w, None, 102.0, 0.04)
    rs = [pf1_attempt(w, t, 0, 0, "a", 5.0, Lat()) for t in tr]
    out["pf1_dublin"] = (len(tr), sum(r["f_pm"] > 0 for r in rs), round(sum(r["pnl"] for r in rs), 4))
    w = synth(correct_ms)
    tr = triggers(w, 112.0, 2.0, 0.04)
    tok = Lat(pm_oneway=110.0, pm_back=110.0, l_oneway=2.0, l_back=2.0, after_pm_ms=2.0)
    rs = [pf1_attempt(w, t, 0, 0, "a", 5.0, tok) for t in tr]
    out["pf1_tokyo"] = (len(tr), sum(r["f_pm"] > 0 for r in rs), round(sum(r["pnl"] for r in rs), 4))
    rs = [probe_replay(w, t.t, t.side, t.lpx, t.ppx, 5.0, ProbeLat()) for t in tr]
    pnl = sum(5 * r.get("pair_cps", 0) / 100 * (r["f_pm"] / 5) + r["unwind_usd"] for r in rs)
    out["lighter_first_tokyo"] = (len(tr), sum(r["f_pm"] > 0 for r in rs), round(pnl, 4))
    return out


def main():
    ok = True
    for c, exp in ((500, dict(pf1_dublin=">0", pf1_tokyo="<=0", lighter_first_tokyo="<=0")),
                   (250, dict(pf1_dublin="<=0", pf1_tokyo="<=0", lighter_first_tokyo="<=0"))):
        res = run(c)
        for k, (n, fills, pnl) in res.items():
            good = pnl > 0 if exp[k] == ">0" else pnl <= 1e-9
            ok &= good
            print(f"lag {c} ms | {k}: triggers {n}, PM fills {fills}, P&L ${pnl} (expect {exp[k]}) {'OK' if good else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
