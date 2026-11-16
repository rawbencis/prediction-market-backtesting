import numpy as np, sys
sys.path.insert(0,'/home/ubuntu/dublin-retest/repo'); import dsim
for d,h in (('2026-11-15',3),('2026-11-15',14),('2026-11-15',20)):
  x=np.fromfile(f'/home/ubuntu/shadow-rec/{d}/pm-{h:02d}.bin',dsim.PM_DT); x=x[(x['tf']==5)&(x['x']>0)]
  out=[]
  for a,name in enumerate(dsim.ASSETS):
    y=x[x['a']==a]; lag=y['l']/1000-y['x']
    out.append(f"{name} p50 {np.percentile(lag,50):.1f} p90 {np.percentile(lag,90):.1f} p99 {np.percentile(lag,99):.0f}")
  print(d,h,' | '.join(out))
