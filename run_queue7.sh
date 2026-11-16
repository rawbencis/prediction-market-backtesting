#!/bin/bash
# placebo control for F7i (BTC 5m, dublin, dev): pull on favourable alt moves. Never starts a job after 00:05 UTC.
cd ~/overnight-wt
. ./guard.sh
while pgrep -f "run_queue[23456].sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_PLACEBO=1 $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do
  hm=$(date -u +%H%M); [ "$hm" -ge 0005 ] && [ "$hm" -lt 0600 ] && { echo "skip $d (late)"; continue; }
  wait_ok; $cap f7_altpull.py $d dublin improve 2>&1 | tail -1
done
echo QUEUE7 DONE
