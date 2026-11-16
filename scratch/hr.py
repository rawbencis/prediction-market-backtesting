import pandas as pd, numpy as np, pmlat
D=pd.concat([pd.read_csv(pmlat.OUT/f'f7i-dublin-{d}-strict.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
C=D[(D.rules=='out+alt+ttl')&(D.thr==50)&(D.model=='pess')]; F=C[C.filled>0].copy()
F['hour']=(F.x0//3600000)%24
g=F.groupby(['day','hour']).apply(lambda G: pd.Series({'n':len(G),'mo1':G.mo1000.mean(),'settle':(G.settle_c*G.filled).sum()/G.filled.sum()}))
print('day-hours',len(g),'mo1>0',(g.mo1>0).sum(),'settle>0',(g.settle>0).sum()); print(g.mo1.describe().round(2).to_dict())
print(F.groupby('n_alts').apply(lambda G: pd.Series({'n':len(G),'mo1':G.mo1000.mean()})).round(2))
F['band']=pd.cut(F.px,[0,2000,4000,6000,8000,10000]); print(F.groupby('band',observed=True).apply(lambda G: pd.Series({'n':len(G),'mo1':G.mo1000.mean(),'settle':(G.settle_c*G.filled).sum()/G.filled.sum()})).round(2))
