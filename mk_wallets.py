"""15m makers follow-up: every BTC 15m fill of the given wallets (RTDS census), Polymarket data only.
Per fill: role (maker = no fee; census fee -1), side, token, price, size, match time (trade print of the same tx), seconds left,
settled P&L, token-mid markouts at 1/5/30/60 s (only while the book is two-sided and > 30 s remain after the markout time),
and act_rec._book_features (tick vs best bid, spread, imbalance, level size, ms since the best prices changed, own 1 s move,
largest alt 15m UP-mid move over 500 ms) on the book standing just before the match.
Usage: python mk_wallets.py WALLET [WALLET ...] -- DAY [DAY ...] -> out/mk/fills.csv.gz
"""
import gzip
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from act_rec import ACT_DT, _book_features  # before pmlat: pmlat puts ~/dublin-retest/repo (older act_rec.py) first on sys.path
import pmlat
import dsim

ADT = np.dtype(ACT_DT)
REC = Path.home() / "shadow-rec"
OUT = Path(__file__).resolve().parent / "out" / "mk"
MOS = (1000, 5000, 30000, 60000)


def shift(day, h, dh):
    t = date.fromisoformat(day)
    h2 = h + dh
    if h2 < 0:
        return (t - timedelta(1)).isoformat(), h2 + 24
    if h2 > 23:
        return (t + timedelta(1)).isoformat(), h2 - 24
    return day, h2


def rd(kind, dt, day, h, keep):
    if pmlat.MAXDAY and day > pmlat.MAXDAY:
        return np.zeros(0, dt)
    p = REC / day / f"{kind}-{h:02d}.bin"
    if not p.exists():
        return np.zeros(0, dt)
    x = np.fromfile(p, dtype=dt)
    return x[keep(x)]


def main():
    k = sys.argv.index("--")
    wallets = [w.lower().removeprefix("0x") for w in sys.argv[1:k]]
    days = sys.argv[k + 1:]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for day in days:
        for f in sorted((REC / day).glob("act-*.bin")):
            h = int(f.stem.split("-")[1])
            a = np.fromfile(f, dtype=ADT)
            a = a[(a["a"] == 0) & (a["tf"] == 15)]
            wh = np.array([w.ljust(20, b"\0").hex() for w in a["w"]])
            sel = np.zeros(len(a), bool)
            for w in wallets:
                sel |= np.char.startswith(wh, w)
            if not sel.any():
                continue
            me, mw = a[sel], wh[sel]
            tx = me["tx"]
            tr = np.concatenate([rd("tr", dsim.TR_DT, *shift(day, h, dh), lambda x: np.isin(x["tx"], tx)) for dh in (0, 1)])
            xms = {bytes(t): int(x) for t, x in zip(tr["tx"], tr["x"])}
            eps = np.unique(me["e"])
            pm = np.concatenate([rd("pm", dsim.PM_DT, *shift(day, h, dh), lambda x: (x["tf"] == 15) & np.isin(x["e"], eps)) for dh in (-1, 0, 1)])
            pm = pm[np.lexsort((pm["x"], pm["e"], pm["a"]))]
            books = {}
            for (aa, e) in set(zip(pm["a"].tolist(), pm["e"].tolist())):
                books[(aa, e)] = pm[(pm["a"] == aa) & (pm["e"] == e)]
            for r, w in zip(me, mw):
                e = int(r["e"])
                x = xms.get(bytes(r["tx"]), int(r["x"]))
                tok, p, end = int(r["tok"]), float(r["p"]), (e + 900) * 1000
                pp = books.get((0, e))
                rec = dict(w=w[:10], day=day, e=e, x=x, left=(end - x) / 1000, tok=tok, sd=int(r["sd"]), p=p, sz=float(r["sz"]),
                           role="taker" if r["fee"] > 0 else "maker", fee=max(float(r["fee"]), 0.0))
                if pp is not None and len(pp):
                    i = int(np.searchsorted(pp["x"], x - 1, "right")) - 1
                    if i >= 0:
                        others = [books[(aa, e)] for aa in range(1, 5) if (aa, e) in books]
                        rec.update(_book_features(pp, i, x, tok, p, others) or {})
                        for hh in MOS:
                            t = x + hh
                            j = int(np.searchsorted(pp["x"], t, "right")) - 1
                            ok = t < end - 30000 and j >= 0 and pp["b"][j] > 0 and pp["k"][j] > pp["b"][j]
                            if ok:
                                mid = (int(pp["b"][j]) + int(pp["k"][j])) / 2e4
                                mt = mid if tok == 0 else 1 - mid
                                rec[f"mo{hh}"] = 100 * ((mt - p) if r["sd"] == 0 else (p - mt))  # c/share, signed for the wallet
                rows.append(rec)
        print(day, len(rows), flush=True)
    D = pd.DataFrame(rows)
    W = pmlat.winners(sorted({f"btc-updown-15m-{e}" for e in D.e.unique()}))
    D["winner"] = D.e.map(lambda e: W.get(f"btc-updown-15m-{e}"))
    pay = np.where((D.winner == "UP") == (D.tok == 0), 1.0, 0.0)
    D["won"] = np.where(D.winner.isna(), np.nan, pay)
    D["pnl"] = np.where(D.sd == 0, D.sz * (pay - D.p), D.sz * (D.p - pay)) - D.fee
    D.loc[D.winner.isna(), "pnl"] = np.nan
    D["settle_c"] = 100 * D.pnl / D.sz
    with gzip.open(OUT / "fills.csv.gz", "wt") as fo:
        D.to_csv(fo, index=False)
    print("wrote", len(D), D.groupby("w").size().to_dict())


if __name__ == "__main__":
    main()
