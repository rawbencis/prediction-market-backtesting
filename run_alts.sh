#!/bin/bash
# live `bid` rule on 5m books: BTC 11-14..17 (calibration) and ETH/SOL/XRP/DOGE dev 11-14..16. One job at a time, capped.
# Holdout (alts 11-17) runs only via run_alts_ho.sh, after the analysis code is committed.
cd ~/overnight-wt
. ./guard.sh
run() { wait_ok; echo "start $1 $2 $(date -u +%T)"; systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=$3 PMLAT_STRICT=1 $HOME/shadow-venv/bin/python alt_bid.py $1 $2 2>&1 | tail -1; }
for d in 2026-11-14 2026-11-15 2026-11-16 2026-11-17; do run $d btc 2026-11-17; done
for a in eth sol xrp doge; do for d in 2026-11-14 2026-11-15 2026-11-16; do run $d $a 2026-11-16; done; done
echo ALTS DEV DONE $(date -u +%T)
