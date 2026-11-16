import pandas as pd, numpy as np, pmlat
for sfx in ('','-s25'):
  D=pd.concat([pd.read_csv(pmlat.OUT/f'f7i-dublin-{d}{sfx}.csv.gz').assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
  D['own']=np.where(D.tok==0,D.imb,-D.imb)
  for lab,C in (('A',D[(D.rules=='out+opp+dep+alt+ttl')&(D.thr==50)&(D.model=='pess')&(D.own>0.6)]),('B',D[(D.rules=='out+alt+ttl')&(D.thr==50)&(D.model=='pess')])):
    F=C[C.filled>0]
    sh=F.filled.sum(); pnl=(F.settle_c*F.filled).sum()/100
    print(f'size{sfx or "-s5"} {lab}: fills {len(F)} shares/fill mean {F.filled.mean():.1f} median {F.filled.median():.0f} full {np.mean(F.filled>=(25 if sfx else 5)-1e-6):.2f} settle c/share {(F.settle_c*F.filled).sum()/sh:+.2f} mo1s(sh-wt) {(F.mo1000*F.filled).sum()/F.filled[F.mo1000.notna()].sum():+.2f} $/day {pnl/2:+.0f}')
