# For NEW 'improved' fills: when did the token's spread last open to >= 2 ticks before P appeared as the new best bid?
import sys; sys.path.insert(0, "/home/ubuntu/overnight-wt")
import numpy as np, pandas as pd, orjson
import lvl_report as R, mk_level as L
P = pd.read_csv("/home/ubuntu/overnight-wt/out/lvl/new_prevbest.csv")
P = P[(P.prev > 0) & (P.prev < P.P)]
D = R.load(); D = D[D.nobook.isna() & ~D.e.map(L.bad_window) & (D.cls=="NEW") & D.best_ms.notna() & ~D.sweep.fillna(False).astype(bool)]
D = D.merge(P[["e","P","prev"]].drop_duplicates(), on=["e","P"])
rows = []
for day in R.DAYS:
    dd = D[D.day==day]; eps = set(dd.e.unique()); open_ = {}
    def fin(e):
        mk = open_.pop(e); x, Lr, drop = mk.arrays()
        for _, f in dd[dd.e==e].iterrows():
            BP, _, AP, _ = L.tok_book(Lr, int(f.tok))
            tb = int(f.t_fill) - int(f.best_ms)
            j = int(np.searchsorted(x, tb, "left"))  # first row with P best
            i = j - 1
            if i < 1 or drop[i]: continue
            spr = (AP[:j, 0] - BP[:j, 0])
            narrow = np.flatnonzero((spr <= 100) | (BP[:j, 0] <= 0) | (AP[:j, 0] <= 0) | drop[:j])
            k = int(narrow[-1]) + 1 if len(narrow) else 0  # first row of the current >= 2-tick stretch
            if k >= j: continue
            opened_by = "bid fell" if BP[k, 0] < BP[k-1, 0] else ("ask rose" if AP[k, 0] > AP[k-1, 0] else "other")
            rows.append(dict(w=f.w, day=day, e=e, sz=f.sz, mo1000=f.mo1000, mo5000=f.mo5000, settle_c=f.settle_c,
                             gap_ms=int(x[j]) - int(x[k]), spr_c=int(spr[i]) / 100, opened_by=opened_by))
    for d2, h in L.hours_for(day):
        p = L.FR / d2 / f"pmbook-{h:02d}.jsonl.gz"
        if p.exists():
            for line in L.lines15(p):
                dct = orjson.loads(line); e = int(dct["slug"].rsplit("-",1)[1])
                if e in eps: open_.setdefault(e, L.Mk()).add(dct)
        hend = int(pd.Timestamp(d2, tz="UTC").timestamp()) + 3600*(h+1)
        for e in [e for e in open_ if e + 900 + 3600 <= hend]: fin(e)
    for e in list(open_): fin(e)
    print(day, len(rows), flush=True)
G = pd.DataFrame(rows); G.to_csv("/home/ubuntu/overnight-wt/out/lvl/new_gap.csv", index=False)
for w, g in G.groupby("w"):
    print(w, "n", len(g), "gap ms p10/p25/p50/p75/p90", g.gap_ms.quantile([.1,.25,.5,.75,.9]).round(0).tolist(), "spread before c p50", g.spr_c.median(), g.opened_by.value_counts(normalize=True).round(2).to_dict())
    for lo, hi in [(0,100),(100,250),(250,1000),(1000,5000),(5000,10**9)]:
        gg = g[(g.gap_ms>=lo)&(g.gap_ms<hi)]
        if len(gg): print(f"   gap {lo}-{hi} ms: share of shares {gg.sz.sum()/g.sz.sum():.2f}, mo1 {R.wavg(gg.mo1000,gg.sz):+.2f}, mo5 {R.wavg(gg.mo5000,gg.sz):+.2f}, settle {R.wavg(gg.settle_c,gg.sz):+.2f}")
