import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',230)
D=pd.concat([pd.read_csv(pmlat.OUT/f'f3i-btc5-dublin-{d}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
D['own']=np.where(D.tok==0,D.imb,-D.imb)
C=D[(D.rules=='out+opp+dep+ttl')&(D.model=='pess')&(D.own>0.6)].copy()
F=C[C.filled>0].copy()
print('orders',len(C),'fills',len(F),'fill rate',round(len(F)/len(C),3),'full 5',(F.filled>=5).mean().round(3),'filled shares mean',F.filled.mean().round(2))
F['hour']=(F.x0//3600000)%24
F['band']=pd.cut(F.px,[0,2000,4000,6000,8000,10000])
F['tl']=pd.cut(F.tleft0/1000,[0,60,120,180,240,330])
F['w']=F.filled
def s(G): 
    return pd.Series({'n':len(G),'settle':(G.settle_c*G.w).sum()/G.w.sum(),'mo1000':G.mo1000.mean(),'mo5000':G.mo5000.mean(),'exit':G.exit_c.mean()})
print(F.groupby('day').apply(s).round(2))
print(F.groupby('band',observed=True).apply(s).round(2))
print(F.groupby('tl',observed=True).apply(s).round(2))
h=F.groupby('hour').apply(s); print('hours positive settle',(h.settle>0).sum(),'of',len(h),' mo1000>0',(h.mo1000>0).sum())
print('t_live ms q',C.t_live.quantile([.25,.5,.75]).tolist(),' fill delay ms q',(F.tfill-F.x0).quantile([.1,.5,.9]).tolist())
print('spread at event q',F.spr.quantile([.1,.5,.9]).tolist(),'sz0 n/a; side split',F.tok.value_counts().to_dict())
# share-weighted settle CI
lo,hi=pmlat.boot_ci((F.settle_c*F.w).to_numpy(),F.e.to_numpy(),weights=F.w.to_numpy()); print('share-weighted settle',round((F.settle_c*F.w).sum()/F.w.sum(),2),'CI mkt',round(lo,2),round(hi,2))
