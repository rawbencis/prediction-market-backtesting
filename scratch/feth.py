import pandas as pd, numpy as np, pmlat
for scn in ('dublin','tokyo'):
  D=pd.concat([pd.read_csv(pmlat.OUT/f'f7i-eth5-{scn}-{d}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
  D['own']=np.where(D.tok==0,D.imb,-D.imb)
  for lab,C in (('A',D[(D.rules=='out+opp+dep+alt+ttl')&(D.thr==50)&(D.model=='pess')&(D.own>0.6)]),('B',D[(D.rules=='out+alt+ttl')&(D.thr==50)&(D.model=='pess')]),('noalt',D[(D.rules=='out+ttl')&(D.thr==50)&(D.model=='pess')])):
    F=C[C.filled>0].reset_index(drop=True); v=F.settle_c.dropna(); lm=pmlat.boot_ci(v.to_numpy(),F.loc[v.index].e.to_numpy())
    print(f'eth5 {scn} {lab}: orders {len(C)} fills {len(F)} mo1s {F.mo1000.mean():+.2f} mo5s {F.mo5000.mean():+.2f} settle {v.mean():+.2f} [{lm[0]:+.2f},{lm[1]:+.2f}]')
