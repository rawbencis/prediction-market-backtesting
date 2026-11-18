#!/bin/bash
# ladder follow-up steps 0-1: level history of the two wallets' BTC 15m maker fills (feeds-rec 5-level book), one job, capped
cd ~/overnight-wt
. ./guard.sh
for d in ${LVL_DAYS:-2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18}; do
  wait_ok; echo "start lvl $d $(date -u +%T)"
  systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python mk_level.py $d 2>&1 | tail -1
done
echo LVL DONE $(date -u +%T)
