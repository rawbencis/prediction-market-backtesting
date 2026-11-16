import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',230)
cols=['e','tok','imb','rules','model','filled','mo1000','settle_c','thr','t_live']
for pfx in ('f7',):
  D=pd.concat([pd.read_csv(pmlat.OUT/f'{pfx}-dublin-{d}.csv.gz',usecols=cols).assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
  D['own']=np.where(D.tok==0,D.imb,-D.imb)
  rows=[]
  for (rules,thr,model),G0 in D[D.model.isin(['pess','prop'])].groupby(['rules','thr','model']):
    for ib in ('any','own>0.6'):
      G=G0 if ib=='any' else G0[G0.own>0.6]
      F=G[G.filled>0].reset_index(drop=True)
      r=dict(mode=pfx,rules=rules,thr=thr,model=model,ib=ib,orders=len(G),fills=len(F),live=G.t_live.median(),mo1=F.mo1000.mean())
      v=F.settle_c.dropna(); lm=pmlat.boot_ci(v.to_numpy(),F.loc[v.index].e.to_numpy())
      r.update(settle=v.mean(),lo=lm[0],hi=lm[1]); rows.append(r)
  R=pd.DataFrame(rows); print(R.round(2).to_string(index=False)); R.to_csv(pmlat.OUT/f'{pfx}grid-dev.csv',index=False)
