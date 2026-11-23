"""Dump one wallet's census fills for one day (Polymarket-only: --no-binance) as a pickle of row dicts.
usage: d1_rows.py WALLET_PREFIX ASSET TF DAY OUT.pkl"""
import pickle, sys, types
from pathlib import Path
sys.path.insert(0, str(Path.home() / "overnight-wt"))
import act_rec
cap = []
act_rec._fills_summary = lambda rows, out=print, binance=True: cap.extend(rows)
w, asset, tf, day, out = sys.argv[1:6]
a = types.SimpleNamespace(rec=Path.home() / "shadow-rec", days=[day], asset=asset, tf=int(tf), wallet=w, summary=True,
                          no_binance=True, settle=Path.home() / "dublin-1124/settle.csv", no_fetch=True)
act_rec.cmd_fills(a)
pickle.dump(cap, open(out, "wb"))
print(day, w, len(cap), "rows")
