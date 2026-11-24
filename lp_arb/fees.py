"""Polymarket taker fee rate per 15m window, as actually charged.

Sources, in order:
  1. Dublin's trade census (`reports/dublin/fees_by_window.csv` on branch research/dublin-1125,
     columns asset,tf,window_start_utc,fee_rate,source,n_taker_rows): the median over the window's
     taker rows of fee / (size * p * (1 - p)). Covers 2026-11-14 ~21:40 -> 2026-11-24 10:55 UTC
     (the census recorder on Dublin was stopped on 11-24).
  2. Windows with no census row:
       - starting before the switch (2026-11-21 00:03 UTC): 0.07. Every census window before the
         switch, on every asset, paid 0.07; Gamma's current feeSchedule (0.01 on older markets) is
         known to be wrong for this period.
       - after the switch: Gamma's feeSchedule rate for that market (matches the census after the
         switch: BTC 0.05, ETH 0.01).
  3. If Gamma has no rate either: BTC 0.05 / others 0.01 after the switch.

The fee formula is rate * p * (1 - p), taker only, as in the frozen rule. Maker rebates are taken as 0.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "lp_arb" / "census" / "fees_by_window.csv"
SWITCH_E = int(datetime(2026, 11, 21, 0, 3, tzinfo=timezone.utc).timestamp())  # clock time of the 0.07 -> new-rate switch
PRE_SWITCH_RATE = 0.07


def load_census(path=DEFAULT_CSV, tf="15m"):
    """{(asset, window_start_epoch): rate} for census rows of the given timeframe."""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open() as f:
        for r in csv.DictReader(f):
            if r["tf"] != tf or r["source"] != "census" or not r["fee_rate"]:
                continue
            e = int(datetime.strptime(r["window_start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
            out[(r["asset"], e)] = float(r["fee_rate"])
    return out


def rate_for(asset, e, census, gamma_rate=float("nan")):
    """(rate, source) for window start e (epoch s) of asset ('btc'/'eth')."""
    if (asset, e) in census:
        return census[(asset, e)], "census"
    if e < SWITCH_E:
        return PRE_SWITCH_RATE, "pre-switch-0.07"
    if gamma_rate == gamma_rate:
        return gamma_rate, "gamma"
    return (0.05 if asset == "btc" else 0.01), "default"
