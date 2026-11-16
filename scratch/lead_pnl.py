import pandas as pd, numpy as np, pmlat, f1_grid
D=f1_grid.load('dublin',['2026-11-14','2026-11-15'])
D=D[(D.filled==1)]
L=['d15696efc99cf15c3e2f070769780c90917de56f','eebde7a0e019a63e6b476eb425505b7b3e6eba30','50ea9d870f0347402afbf9d42713bb857bda5963','84389cfc4a652ea14d8d8be969769b1f69de3680','129246beaac356e104c397f0985c11b7469be5a6','095fd7cc9ddf7110586d1bda3974eccc52155f24']
for w in L:
  for side in ('follow','fade'):
    G=f1_grid.dedupe(D[(D.w==w)&(D.side==side)])
    if len(G)<10: continue
    s=[f"{ex} {100*G[ex].mean():+.2f}" for ex in ('settle','tk1000','tk5000','mk100')]
    lo,hi=pmlat.boot_ci(100*G.settle.dropna().to_numpy(),G.loc[G.settle.notna()].e.to_numpy())
    print(w[:8],side,len(G),' '.join(s),f'settle CI [{lo:+.1f},{hi:+.1f}]', 'usd med',round(G.usd.median()),'sz mode',G.sz.round(1).mode().tolist()[:3] if 'sz' in G else '')
# wallet-level: how many wallets with >=30 dedupe events have tk5000 mean>0 (noise check)
res=[]
for w,G in D[D.side=='follow'].groupby('w'):
    G=f1_grid.dedupe(G)
    if len(G)>=30: res.append((w,len(G),100*G.tk5000.mean(),100*G.settle.mean()))
R=pd.DataFrame(res,columns=['w','n','tk5000','settle'])
print('wallets>=30 events',len(R),'tk5000>0:',(R.tk5000>0).sum(),'settle>0:',(R.settle>0).sum())
print(R.sort_values('tk5000',ascending=False).head(8).round(2).to_string(index=False))
