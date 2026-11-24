"""D1 switch: first window at the new fee rate per asset x tf, and window-creation vs clock-time test.

Reads the taker rows saved by d1_fees.py (--rows-out). Prints a markdown table.
For every window whose rows have both old- and new-rate fills, shows the clock time of each class.
"""
import sys, time
import numpy as np

ASSETS = ("btc", "eth", "sol", "xrp", "doge")
z = np.load(sys.argv[1])
x, a, tf, e, rate = z["x"], z["a"], z["tf"], z["e"], z["rate"]
T = lambda s: time.strftime("%m-%d %H:%M:%S", time.gmtime(s))
for ai, asset in enumerate(ASSETS):
    for t in (5, 15):
        m = (a == ai) & (tf == t)
        ee, rr, xx = e[m], rate[m], x[m] // 1000
        old = np.abs(rr - 0.07) < 0.005
        new_rate = 0.05 if asset == "btc" else 0.01
        new = np.abs(rr - new_rate) < 0.005 if asset == "btc" else np.abs(rr - new_rate) < 0.003
        # per-window class
        ue = np.unique(ee)
        cls = {}
        for w in ue:
            k = ee == w
            no, nn = int(old[k].sum()), int(new[k].sum())
            cls[w] = (no, nn, int(k.sum()))
        last_old_w = max((w for w in ue if cls[w][0] > 0), default=None)
        first_new_w = min((w for w in ue if cls[w][1] > 0 and cls[w][1] >= cls[w][0]), default=None)
        mixed = [w for w in ue if cls[w][0] > 0 and cls[w][1] > 0]
        print(f"## {asset} {t}m  rows={m.sum()} old(0.07)={old.sum()} new({new_rate})={new.sum()} other={(~old & ~new).sum()}")
        print(f"   last window with any 0.07 row: {T(last_old_w) if last_old_w else '-'}  first window majority-new: {T(first_new_w) if first_new_w else '-'}")
        print(f"   last 0.07 row clock: {T(xx[old].max()) if old.any() else '-'}  first new row clock: {T(xx[new].min()) if new.any() else '-'}")
        for w in mixed[:6]:
            k = ee == w
            print(f"   mixed window {T(w)} old={cls[w][0]} [{T(xx[k & old].min())}..{T(xx[k & old].max())}] new={cls[w][1]} [{T(xx[k & new].min())}..{T(xx[k & new].max())}]")
        if len(mixed) > 6: print(f"   ... {len(mixed)} mixed windows")
        # clock-time overlap: any new-rate row in a window starting before first_new_w, any old row after
        if first_new_w:
            pre = (ee < first_new_w) & new; post = (ee >= first_new_w) & old
            print(f"   new-rate rows in earlier windows: {pre.sum()}  old-rate rows in later windows: {post.sum()}")
            # rows at the same clock time in different windows (15m windows overlap clock with 5m? no: same asset tf windows don't overlap)
