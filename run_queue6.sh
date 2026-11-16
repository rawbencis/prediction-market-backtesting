#!/bin/bash
# control: F7i with ETH 5m as the target (triggers = BTC/SOL/XRP/DOGE 5m). Never starts a job after 00:05 UTC (holdout night window).
cd ~/overnight-wt
. ./guard.sh
while pgrep -f "run_queue[2345].sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for sc in dublin tokyo; do for d in 2026-11-14 2026-11-15; do
  hm=$(date -u +%H%M); [ "$hm" -ge 0005 ] && [ "$hm" -lt 0600 ] && { echo "skip $sc $d (late)"; continue; }
  wait_ok; $cap f7_altpull.py $d $sc improve eth 5 2>&1 | tail -1
done; done
echo QUEUE6 DONE
