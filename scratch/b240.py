import pandas as pd, numpy as np, pmlat
pd.set_option('display.width',220)
D=pd.concat([pd.read_csv(pmlat.OUT/f'ev-btc5-{d}.csv.gz',usecols=['e','tp','dir','usd','px','up','w','mid_before','mv1000','mv5000','mv30000','swept','b0','k0'],dtype={'w':str}).assign(day=d) for d in ('2026-11-14','2026-11-15')])
D['sec']=(D.tp-D.e*1000)/1000
D['fav']=np.where(D.mid_before>=5000,1,-1)   # +1 if UP is favourite
D['withfav']=(D.dir==D.fav)
for lo,hi in ((236,239),(239,240),(240,240.25),(240.25,240.5),(240.5,241),(241,243),(243,246)):
    G=D[(D.sec>=lo)&(D.sec<hi)]
    print(f'{lo}-{hi}s n {len(G)} usd {G.usd.sum():.0f} withfav$ {G[G.withfav].usd.sum()/max(G.usd.sum(),1):.2f} med px {G.px.median()} mv1k {G.mv1000.mean():.1f} mv30k {G.mv30000.mean():.1f}')
G=D[(D.sec>=240)&(D.sec<240.5)]
print('top wallets 240.0-240.5', G.groupby('w').usd.agg(['size','sum']).sort_values('sum',ascending=False).head(6).round(0).to_dict('index'))
print('ms after 240 s, q', ((G.sec-240)*1000).quantile([.1,.25,.5,.75,.9]).round(0).tolist())
# per market: burst in [240,240.5) $ and direction purity
M=G.groupby(['day','e']).agg(usd=('usd','sum'),n=('usd','size'),fav=('withfav','mean'))
print('markets with burst',len(M),'of',D.groupby(['day','e']).ngroups,'usd q',M.usd.quantile([.25,.5,.75,.9]).round(0).tolist(),'withfav share',M.fav.mean().round(2))
# favourite price at 239.9: distribution
