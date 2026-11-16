import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',220)
D=pd.concat([pd.read_csv(pmlat.OUT/f'f4-dublin-{d}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
print('F4 events',len(D))
D['ub']=pd.cut(D.usd,[50,100,250,500,1e9])
print(D.groupby(['swept','ub'],observed=True)[['mv100','mv1000','mv5000','mv30000','settle','tk5000','tk30000']].agg(['mean']).round(2))
print(D.groupby(['swept','ub'],observed=True).size())
n=0
for sw in (0,1):
  for mu in (50,100,250,500):
    G=D[(D.usd>=mu)&((D.swept==1) if sw else True)].drop_duplicates(['e15']).reset_index(drop=True) # first per 15m market
    for ex in ('settle','tk5000','tk30000'):
      v=G[ex].dropna(); n+=1
      if len(v)>20:
        lo,hi=pmlat.boot_ci(v.to_numpy(),G.loc[v.index].e15.to_numpy())
        if v.mean()>0: print('pos cfg',sw,mu,ex,len(v),round(v.mean(),2),round(lo,2),round(hi,2))
print('F4 configs',n)
F=pd.concat([pd.read_csv(pmlat.OUT/f'f5-dublin-{d}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
print('\nF5 rows',len(F))
F['ib']=pd.cut(F.imb,[-1,-.8,-.5,-.2,.2,.5,.8,1])
print(F.groupby(['ib'],observed=True)[['dm100','dm1000','dm5000']].mean().round(3))
print(F[F.spr==100].groupby(['ib'],observed=True)[['dm100','dm1000','dm5000']].mean().round(3))
T=F[F.px.notna()].reset_index(drop=True)
print('F5 taker trades',len(T), T[['settle','tk1000','tk5000']].mean().round(2).to_dict())
for ex in ('settle','tk1000','tk5000'):
  v=T[ex].dropna(); lo,hi=pmlat.boot_ci(v.to_numpy(),T.loc[v.index].e.to_numpy()); print(ex,len(v),round(v.mean(),2),round(lo,2),round(hi,2))
