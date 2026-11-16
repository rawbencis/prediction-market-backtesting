import pandas as pd, numpy as np, pmlat
def ev(fname_tpl, lab):
  D=pd.concat([pd.read_csv(pmlat.OUT/fname_tpl.format(d=d)).assign(day=d) for d in ('2026-11-14','2026-11-15')],ignore_index=True)
  D['own']=np.where(D.tok==0,D.imb,-D.imb)
  for k,C in (('A',D[(D.rules=='out+opp+dep+alt+ttl')&(D.thr==50)&(D.model=='pess')&(D.own>0.6)]),('B',D[(D.rules=='out+alt+ttl')&(D.thr==50)&(D.model=='pess')]),('ctl out+ttl',D[(D.rules=='out+ttl')&(D.thr==50)&(D.model=='pess')])):
    F=C[C.filled>0].reset_index(drop=True); w=F.filled; v=F.settle_c
    lm=pmlat.boot_ci((v*w).to_numpy(),F.e.to_numpy(),weights=w.to_numpy())
    mo=(F.mo1000*w).sum()/w[F.mo1000.notna()].sum()
    print(f'{lab:18} {k:12} orders {len(C):6} fills {len(F):6} sh/fill {w.mean():4.1f} live {C.t_live.median():5.0f} mo1s {mo:+.2f} settle {(v*w).sum()/w.sum():+.2f} [{lm[0]:+.2f},{lm[1]:+.2f}] $/day {(v*w).sum()/200:+.0f}')
ev('f7i-dublin-{d}.csv.gz','real alt pull')
ev('f7i-placebo-dublin-{d}.csv.gz','PLACEBO pull')
ev('f7i-dublin-{d}-strict.csv.gz','strict s5')
ev('f7i-tokyo-{d}-strict.csv.gz','strict s5 tokyo')
ev('f7i-dublin-{d}-s25-strict.csv.gz','strict s25')
