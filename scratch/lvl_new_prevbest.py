# For NEW fills (P best at the fill, not a sweep): the token best bid on the row before P became best. Streams pmbook for the
# NEW fills' windows only, one day at a time.
import sys; sys.path.insert(0, "/home/ubuntu/overnight-wt")
import numpy as np, pandas as pd, orjson
import lvl_report as R, mk_level as L
D = R.load(); D = D[D.nobook.isna() & ~D.e.map(L.bad_window) & (D.cls=="NEW") & D.best_ms.notna() & ~D.sweep.fillna(False).astype(bool)]
rows = []
for day in R.DAYS:
    dd = D[D.day==day]; eps = set(dd.e.unique()); open_ = {}
    def fin(e):
        mk = open_.pop(e); x, Lr, drop = mk.arrays()
        for _, f in dd[dd.e==e].iterrows():
            BP = L.tok_book(Lr, int(f.tok))[0]
            tb = int(f.t_fill) - int(f.best_ms)
            i = int(np.searchsorted(x, tb, "left")) - 1  # row before P became best
            if i < 0 or drop[i]: continue
            prev = int(BP[i, 0])
            rows.append(dict(w=f.w, day=day, P=int(f.P), prev=prev, sz=f.sz, mo1000=f.mo1000, settle_c=f.settle_c, e=e))
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
P = pd.DataFrame(rows); P["how"] = np.where(P.prev <= 0, "no bid", np.where(P.prev < P.P, "improved (prev best below P)", np.where(P.prev > P.P, "level above removed (prev best above P)", "same")))
for w, g in P.groupby("w"):
    print(w)
    for h, gg in g.groupby("how"):
        print(f"  {h}: share of NEW shares {gg.sz.sum()/g.sz.sum():.2f}, n {len(gg)}, ticks |P-prev| p50 {abs(gg.P-gg.prev).median()/100:.0f}, mo1 {R.wavg(gg.mo1000, gg.sz):+.2f}, settle {R.wavg(gg.settle_c, gg.sz):+.2f}")
P.to_csv("/home/ubuntu/overnight-wt/out/lvl/new_prevbest.csv", index=False)
