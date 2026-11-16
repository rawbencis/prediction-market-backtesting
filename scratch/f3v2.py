import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',230)
cols=['e','spr','tleft0','imb','tok','px','rules','model','filled','mo1000','settle_c','exit_c']
def load(pfx): return pd.concat([pd.read_csv(pmlat.OUT/f'{pfx}-btc5-dublin-{d}.csv.gz',usecols=cols).assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
J=load('f3'); I=load('f3i')
# heavy side: our side's imbalance: side bid(tok0): imb>thr ; offer (tok1): imb<-thr
for name,D in (('join',J),('improve',I)):
  D['own']=np.where(D.tok==0,D.imb,-D.imb)
  rows=[]
  for (rules,model),G0 in D.groupby(['rules','model']):
    for ib in ('any','own>0.3','own>0.6','own<-0.3'):
      for spr in ('any','ge2t'):
        G=G0
        if ib=='own>0.3': G=G[G.own>0.3]
        if ib=='own>0.6': G=G[G.own>0.6]
        if ib=='own<-0.3': G=G[G.own<-0.3]
        if spr=='ge2t': G=G[G.spr>=200]
        F=G[G.filled>0].reset_index(drop=True)
        for ex in ('settle_c','mo1000','exit_c'):
          v=F[ex].dropna()
          if len(v)<20: continue
          lm=pmlat.boot_ci(v.to_numpy(),F.loc[v.index].e.to_numpy()); ld=pmlat.boot_ci(v.to_numpy(),F.loc[v.index].day.to_numpy())
          rows.append(dict(mode=name,rules=rules,model=model,ib=ib,spr=spr,exit=ex,orders=len(G),fills=len(v),net=v.mean(),lo_m=lm[0],hi_m=lm[1],lo_d=ld[0],hi_d=ld[1]))
  R=pd.DataFrame(rows); R.to_csv(pmlat.OUT/f'f3v2grid-{name}.csv',index=False)
  print(f'== {name}: configs {len(R)}')
  print(R[R.exit=='mo1000'].pivot_table(index=['rules','ib','spr'],columns='model',values='net').round(2).to_string())
  X=R[R.model.isin(['pess','prop'])&(R.exit=='settle_c')].sort_values('net',ascending=False)
  print(X.head(6).round(2).to_string(index=False))
