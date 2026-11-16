import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',220)
D=pd.concat([pd.read_csv(pmlat.OUT/f'f6-dublin-{d}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
A=D[(D.kind=='A')&(D.filled>0)&(D.model=='pess')]
print(A.groupby(['tpost','K','C'])[['settle_c','mo1000','mo30000','tfill','fav_ask','spr']].agg(['mean','median']).round(1).to_string())
G=A[(A.tpost==239.0)&(A.K==0)&(A.C==0.5)]
print(G[['day','e','fav','fav_ask','spr','tfill','settle_c','mo1000','mo30000']].sort_values('mo30000').to_string())
