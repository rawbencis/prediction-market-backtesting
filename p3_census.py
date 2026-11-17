"""15m study, Part 3i: who is profitable on BTC 15m? RTDS activity census (act rows, one per fill participant), streamed per hour.
Per wallet and role (maker = fee 0, taker = fee > 0): settled P&L per $ traded (BUY: size (pay - p); SELL: size (p - pay)),
market-bootstrap 90% CI, markets, $ volume. Then the two steadiest makers (highest CI lower bound, >= 30 markets, >= $2,000):
settled c/share by token price band and by minutes left at the fill. No Binance data.
Usage: PMLAT_MAXDAY=... python p3_census.py DAY [DAY ...] -> stdout markdown
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat
from act_rec import ACT_DT

ADT = np.dtype(ACT_DT)


def load(days, tf=15):
    parts = []
    for d in days:
        for f in sorted((Path.home() / "shadow-rec" / d).glob("act-*.bin")):
            a = np.fromfile(f, dtype=ADT)
            a = a[(a["a"] == 0) & (a["tf"] == tf)]
            parts.append(pd.DataFrame({"x": a["x"], "e": a["e"].astype(np.int64), "tok": a["tok"], "sd": a["sd"], "p": a["p"].astype(float),
                                       "sz": a["sz"].astype(float), "fee": a["fee"].astype(float), "w": [w.hex() for w in a["w"]], "day": d}))
    return pd.concat(parts, ignore_index=True)


def main():
    days = sys.argv[1:]
    R = load(days)
    W = pmlat.winners(sorted({f"btc-updown-15m-{e}" for e in R.e.unique()}))
    R["win"] = R.e.map(lambda e: W.get(f"btc-updown-15m-{e}"))
    R = R[R.win.notna() & (R.sz > 0)].copy()
    pay = np.where((R.win == "UP") == (R.tok == 0), 1.0, 0.0)
    buy = R.sd == 0
    R["pnl"] = np.where(buy, R.sz * (pay - R.p), R.sz * (R.p - pay)) - R.fee
    R["usd"] = R.sz * R.p
    R["role"] = np.where(R.fee > 0, "taker", "maker")
    R["mkt"] = R.day + R.e.astype(str)
    print(f"BTC 15m census rows {len(R):,} in {R.e.nunique()} settled markets, days {days}\n")
    rows = []
    for (w, role), g in R.groupby(["w", "role"]):
        if g.usd.sum() < 2000 or g.mkt.nunique() < 30:
            continue
        lo, hi = pmlat.boot_ci(g.pnl, g.mkt, weights=g.usd)
        rows.append(dict(w=w[:10], wfull=w, role=role, markets=g.mkt.nunique(), usd=g.usd.sum(), buy_share=(g.sd == 0).mean(),
                         c_per_usd=100 * g.pnl.sum() / g.usd.sum(), lo=100 * lo, hi=100 * hi, pnl=g.pnl.sum()))
    T = pd.DataFrame(rows).sort_values("lo", ascending=False)
    print(f"wallet-role pairs with >= $2,000 and >= 30 markets: {len(T)}; makers {int((T.role == 'maker').sum())}, takers {int((T.role == 'taker').sum())}")
    print(f"positive CI lower bound: makers {int(((T.role == 'maker') & (T.lo > 0)).sum())}, takers {int(((T.role == 'taker') & (T.lo > 0)).sum())} "
          f"(~{0.05 * len(T):.1f} expected by chance at one-sided 5%)\n")
    print("| wallet | role | markets | $ traded | buy share | c/$ | 90% CI | P&L $ |\n|---|---|---|---|---|---|---|---|")
    for r in T.head(12).itertuples():
        print(f"| {r.w}… | {r.role} | {r.markets} | {r.usd:,.0f} | {r.buy_share:.2f} | {r.c_per_usd:+.2f} | [{r.lo:+.2f}, {r.hi:+.2f}] | {r.pnl:+,.0f} |")
    M = T[T.role == "maker"].head(2)
    for r in M.itertuples():
        g = R[(R.w == r.wfull) & (R.role == "maker")].copy()
        tp = np.where(g.sd == 0, g.p, 1 - g.p)  # price of the token the wallet ends up long
        g["band"] = pd.cut(tp, [0, 0.2, 0.4, 0.6, 0.8, 1.0001], labels=["0-20", "20-40", "40-60", "60-80", "80-100"], right=False)
        g["mleft"] = pd.cut(((g.e + 900) * 1000 - g.x) / 60000, [-1, 1, 3, 5, 10, 16], labels=["0-1", "1-3", "3-5", "5-10", "10-15"])
        print(f"\nsteady maker {r.w}… ({r.markets} markets, ${r.usd:,.0f}, {r.c_per_usd:+.2f} c/$): settled c/share by band / minutes left")
        for by in ("band", "mleft"):
            s = g.groupby(by, observed=True).apply(lambda z: pd.Series(dict(n=len(z), c_share=100 * z.pnl.sum() / z.sz.sum())), include_groups=False)
            print(s.round(2).to_string())


if __name__ == "__main__":
    main()
