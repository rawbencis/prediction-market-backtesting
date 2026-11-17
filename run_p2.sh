#!/bin/bash
# 15m Part 2 join-maker (and later families), strict fills, one job at a time, 800 MB / 80% CPU cap, nightly guard
cd ~/overnight-wt
. ./guard.sh
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 PMLAT_STRICT=1"
for scn in live dublin; do for d in 2026-11-14 2026-11-15 2026-11-16; do
  wait_ok; echo "start p2 $scn $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p2_join.py $d $scn 2>&1 | tail -1
done; done
echo P2 DONE $(date -u +%T)
