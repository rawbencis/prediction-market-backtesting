import pandas as pd, numpy as np, pmlat
days=('2026-11-14','2026-11-15')
P=pd.read_csv(pmlat.OUT/'f2-wallets-2026-11-14_2026-11-15.csv')
S=set(P[(P.n.between(110,135))&(P.ratio>1.35)].w)
print('swarm candidates',len(S))
D=pd.concat([pd.read_csv(pmlat.OUT/f'ev-btc5-{d}.csv.gz',usecols=['e','tp','tm','dir','usd','sz','px','w','tleft','swept','mv1000','mv5000','mv30000'],dtype={'w':str}).assign(day=d) for d in days])
D['w']=D.w.fillna('')
D['sw']=D.w.isin(S)
X=D[D.sw]
print('swarm prints',len(X),'markets',X.groupby(['day','e']).ngroups)
print('size q',X.sz.quantile([.1,.5,.9]).round(2).tolist(),'usd q',X.usd.quantile([.1,.5,.9]).round(2).tolist(),'px q',X.px.quantile([.1,.5,.9]).tolist())
print('sz value counts', X.sz.round(2).value_counts().head(8).to_dict())
# bursts: cluster swarm prints in same market within 3s
X=X.sort_values(['day','e','tp'])
X['gap']=X.groupby(['day','e']).tp.diff()
X['burst']=((X.gap.isna())|(X.gap>3000)).cumsum()
Bst=X.groupby('burst').agg(day=('day','first'),e=('e','first'),t0=('tp','min'),t1=('tp','max'),n=('w','size'),nw=('w','nunique'),dir=('dir','mean'),usd=('usd','sum'),tleft=('tleft','first'))
print('bursts',len(Bst)); print(Bst.n.describe().round(1).to_dict()); print('dir purity',(Bst.dir.abs()==1).mean())
print('burst duration ms q',(Bst.t1-Bst.t0).quantile([.1,.5,.9]).tolist())
print('tleft at burst s q',(Bst.tleft/1000).quantile([.1,.25,.5,.75,.9]).round(0).tolist())
# what precedes each burst: non-swarm prints in same market same dir within 5 s before t0
rows=[]
for b in Bst.itertuples():
    g=D[(D.day==b.day)&(D.e==b.e)&(~D.sw)&(D.tp<b.t0)&(D.tp>=b.t0-5000)]
    gd=g[np.sign(g.dir)==np.sign(b.dir)]
    if len(gd):
        L=gd.sort_values('usd').iloc[-1]
        rows.append(dict(burst=b.Index,lead_w=L.w,lead_usd=L.usd,lag=b.t0-L.tp,lead_sz=L.sz,lead_px=L.px))
    else: rows.append(dict(burst=b.Index,lead_w=None))
R=pd.DataFrame(rows)
print('bursts with a same-dir non-swarm print in 5 s before:',R.lead_w.notna().mean())
print('top lead wallets', R.lead_w.value_counts().head(8).to_dict())
print('lag ms q',R.lag.quantile([.1,.25,.5,.75,.9]).tolist())
# first wallet in the burst
print('first swarm wallet in burst', X.groupby('burst').w.first().value_counts().head(5).to_dict())
# price impact: mid move in burst direction from just before burst
print('burst usd q', Bst.usd.quantile([.1,.5,.9]).round(0).tolist())
X.to_csv(pmlat.OUT/'swarm_prints_dev.csv.gz',index=False); Bst.to_csv(pmlat.OUT/'swarm_bursts_dev.csv'); R.to_csv(pmlat.OUT/'swarm_leads_dev.csv',index=False)
