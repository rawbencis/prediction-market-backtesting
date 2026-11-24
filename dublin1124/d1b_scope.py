"""Which asset x tf the wallet trades, per day (census act rows). usage: d1b_scope.py WALLET"""
import sys, collections
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path.home() / "overnight-wt"))
from act_rec import ACT_DT, ASSETS
dt = np.dtype(ACT_DT); W = sys.argv[1].lower()[2:]
REC = Path.home() / "shadow-rec"
for d in ["2026-11-%02d" % i for i in range(14, 24)]:
    c = collections.Counter()
    for f in sorted((REC / d).glob("act-*.bin")):
        x = np.fromfile(f, dtype=dt)
        ws = [w for w in np.unique(x["w"]).tolist() if w.ljust(20, b"\0").hex().startswith(W)]
        if not ws: continue
        y = x[np.isin(x["w"], ws)]
        for a, tf, maker, p, sz in zip(y["a"].tolist(), y["tf"].tolist(), (y["fee"] <= 0).tolist(), y["p"].tolist(), y["sz"].tolist()):
            k = (ASSETS[a], tf); c[k + ("n",)] += 1; c[k + ("usd",)] += p * sz; c[k + ("mk",)] += maker
    print(d, {f"{a}{tf}": (c[(a, tf, 'n')], round(c[(a, tf, 'usd')]), round(c[(a, tf, 'mk')] / c[(a, tf, 'n')], 2)) for a, tf, k in sorted(c) if k == "n"}, flush=True)
