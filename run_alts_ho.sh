#!/bin/bash
# HOLDOUT: live `bid` rule on ETH/SOL/XRP/DOGE 5m, 2026-11-17 only. Run once, after alt_bid.py / alt_report.py are committed.
cd ~/overnight-wt
. ./guard.sh
for a in eth sol xrp doge; do
  wait_ok; echo "start $a 2026-11-17 $(date -u +%T)"
  systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-17 PMLAT_STRICT=1 $HOME/shadow-venv/bin/python alt_bid.py 2026-11-17 $a 2>&1 | tail -1
done
echo ALTS HO DONE $(date -u +%T)
