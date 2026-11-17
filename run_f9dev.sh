#!/bin/bash
# F9 dev grid (live scenario), one job at a time, 800 MB cap, nightly-window guard
cd ~/overnight-wt
. ./guard.sh
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16"
for tf in 5 15; do for d in 2026-11-14 2026-11-15; do
  wait_ok; echo "start $tf $d $(date -u +%T) avail $(free -m | awk '/Mem/{print $7}')"
  $cap $HOME/shadow-venv/bin/python f9_grid.py $d live $tf 2>&1 | tail -1
done; done
echo F9DEV DONE $(date -u +%T)
