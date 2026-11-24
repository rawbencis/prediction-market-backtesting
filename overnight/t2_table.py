"""Print T2 cell tables from an agg pickle."""
import pickle
import sys

import numpy as np

rows = pickle.load(open(sys.argv[1], "rb"))
q = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
asset = sys.argv[3] if len(sys.argv) > 3 else "all"
R = [r for r in rows if r["q"] == q and r["asset"] == asset]
print(f"{len(R)} cells at Q={q}, asset={asset}; t>=2 by ratio-SE: {sum(1 for r in R if r['se'] == r['se'] and r['se'] > 0 and abs(r['cps'] / r['se']) >= 2)}; expected by chance {0.0455 * len(R):.1f}")
hdr = "cell | n | both/one/none | pair cost | $ paired / one-leg | unhedged sh | c/share [90%] | t | $/day | P c/sh, L c/sh | P mo1/mo5, L mo1/mo5 (c) | placebo both, sd vs sd"
print(hdr)
for r in sorted(R, key=lambda r: -(r["cps"] if r["cps"] == r["cps"] else -1e9)):
    t = r["cps"] / r["se"] if r["se"] == r["se"] and r["se"] > 0 else np.nan
    ex = ""
    for k in ("P_guess_wrong", "L_guess_wrong", "P_trig", "L_trig", "P_pess_fill", "L_pess_fill", "L_fill140"):
        if k in r:
            ex += f" {k}={r[k]:.2f}" if isinstance(r[k], float) else f" {k}={r[k]}"
    print(f"{r['cell']} | {r['n']} | {r['both']}/{r['one']}/{r['none']} | {r['pair_cost']:.3f} | {r['pnl_paired']:+.2f} / {r['pnl_one']:+.2f} | {r['unhedged']:.1f} | "
          f"{r['cps']:+.2f} [{r['lo']:+.2f}, {r['hi']:+.2f}] | {t:+.1f} | {r['usd_day']:+.1f} | {r['P_cps']:+.2f}, {r['L_cps']:+.2f} | "
          f"{r['P_mo1']:+.1f}/{r['P_mo5']:+.1f}, {r['L_mo1']:+.1f}/{r['L_mo5']:+.1f} | {r['plc_both']:.0f}, {r['sd_window']:.2f} vs {r['plc_sd']:.2f}{ex}")
