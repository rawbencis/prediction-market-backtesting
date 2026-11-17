#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16"
for d in 2026-11-14 2026-11-15 2026-11-16; do wait_ok; echo "start p1 $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p1_15m.py $d 2>&1 | tail -1; done
echo P1 DONE $(date -u +%T)
