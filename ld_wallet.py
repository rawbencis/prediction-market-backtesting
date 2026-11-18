"""Late-dump study, job 1: dissect one wallet on BTC 5m from the RTDS census (act rows) + recorded books/prints. Polymarket only
(act_rec.py fills also loads Binance moves, not allowed on data from 2026-11-15 on, so this replaces it).
Per fill: role (maker = no fee), side, token price, size, seconds left, match time (print of the same tx), the book just before
the match (join = price at the best bid of that token / improve = above it / take = at or above the ask), settlement.
Usage: python ld_wallet.py WALLET_PREFIX DAY [DAY ...] -> out/ld/wallet-{prefix}.csv.gz (one row per fill)
"""
import gzip
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import pmlat
import dsim
from act_rec import ACT_DT

ADT = np.dtype(ACT_DT)
REC = Path.home() / "shadow-rec"
OUT = Path(__file__).resolve().parent / "out" / "ld"


def hour_files(day, kind):
    return sorted((REC / day).glob(f"{kind}-*.bin"))


def main():
    pref, days = sys.argv[1].lower().removeprefix("0x"), sys.argv[2:]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for day in days:
        for f in hour_files(day, "act"):
            a = np.fromfile(f, dtype=ADT)
            a = a[(a["a"] == 0) & (a["tf"] == 5)]
            ws = [w for w in np.unique(a["w"]) if w.ljust(20, b"\0").hex().startswith(pref)]
            if not ws:
                continue
            me = a[np.isin(a["w"], ws)]
            h = f.stem.split("-")[1]
            # match time: the trade print of the same tx (same hour or the next one)
            tr = []
            for d2, h2 in ((day, int(h)), ((date.fromisoformat(day) + timedelta(1)).isoformat(), 0) if int(h) == 23 else (day, int(h) + 1)):
                p = REC / d2 / f"tr-{h2:02d}.bin"
                if p.exists() and (pmlat.MAXDAY is None or d2 <= pmlat.MAXDAY):
                    t = np.fromfile(p, dtype=dsim.TR_DT)
                    tr.append(t[np.isin(t["tx"], me["tx"])])
            tr = np.concatenate(tr) if tr else np.zeros(0, dsim.TR_DT)
            xms = {bytes(t): int(x) for t, x in zip(tr["tx"], tr["x"])}
            # books of the windows touched (this hour's and the previous hour's pm files)
            eps = np.unique(me["e"])
            pm = []
            for d2, h2 in ((day, int(h)), ((date.fromisoformat(day) - timedelta(1)).isoformat(), 23) if int(h) == 0 else (day, int(h) - 1)):
                p = REC / d2 / f"pm-{h2:02d}.bin"
                if p.exists():
                    z = np.fromfile(p, dtype=dsim.PM_DT)
                    pm.append(z[(z["a"] == 0) & (z["tf"] == 5) & np.isin(z["e"], eps)])
            pm = np.concatenate(pm) if pm else np.zeros(0, dsim.PM_DT)
            for r in me:
                e = int(r["e"])
                x = xms.get(bytes(r["tx"]), int(r["x"]))
                B = pm[pm["e"] == e]
                B = B[np.argsort(B["x"], kind="stable")]
                i = int(np.searchsorted(B["x"], x - 1, "right")) - 1
                tok, p_ = int(r["tok"]), float(r["p"])
                bid = ask = np.nan
                if i >= 0 and (B["b"][i] or B["k"][i]):
                    b, k = int(B["b"][i]) / 1e4, int(B["k"][i]) / 1e4
                    bid, ask = (b, k) if tok == 0 else ((1 - k) if k else np.nan, (1 - b) if b else np.nan)
                    bsz = float(B["bs"][i] if tok == 0 else B["ks"][i])
                else:
                    bsz = np.nan
                rows.append(dict(day=day, e=e, x=x, left=(e + 300) - x / 1000, tok=tok, sd=int(r["sd"]), p=p_, sz=float(r["sz"]),
                                 role="taker" if r["fee"] > 0 else "maker", fee=max(float(r["fee"]), 0.0), tx=bytes(r["tx"]).hex()[:16],
                                 bid=bid, ask=ask, bid_size=bsz, w=r["w"].ljust(20, b"\0").hex()))
        print(day, len(rows), flush=True)
    D = pd.DataFrame(rows)
    W = pmlat.winners(sorted({f"btc-updown-5m-{e}" for e in D.e.unique()}))
    D["winner"] = D.e.map(lambda e: W.get(f"btc-updown-5m-{e}"))
    pay = np.where((D.winner == "UP") == (D.tok == 0), 1.0, 0.0)
    D["won"] = np.where(D.winner.isna(), np.nan, pay)
    D["pnl"] = np.where(D.sd == 0, D.sz * (pay - D.p), D.sz * (D.p - pay)) - D.fee
    D.loc[D.winner.isna(), "pnl"] = np.nan
    with gzip.open(OUT / f"wallet-{pref}.csv.gz", "wt") as fo:
        D.to_csv(fo, index=False)
    print("wrote", len(D), "rows; wallets", D.w.unique()[:3])


if __name__ == "__main__":
    main()
