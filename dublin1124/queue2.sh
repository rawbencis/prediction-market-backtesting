#!/bin/bash
# D2 basis-corrected diagnostic, after the main queue.
until grep -q QUEUE_ALL_DONE ~/dublin-1124/queue.log || grep -q "stop" ~/dublin-1124/queue.log; do sleep 30; done
J="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19"; PY=$HOME/shadow-venv/bin/python
cd ~/dublin-1124
DEV="2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18"; HO="2026-11-19 2026-11-20 2026-11-21 2026-11-22 2026-11-23"
for spec in "btc 5 btc5" "btc 15 btc15" "eth 5 eth5"; do set -- $spec
  for half in dev ho; do D=$DEV; [ $half = ho ] && D=$HO
    p90=$(tail -1 ~/shadow-latency.csv | cut -d, -f5); python3 -c "import sys;sys.exit(0 if float('$p90')>60 else 1)" && { echo "p90 stop"; exit 1; }
    $J $PY d2_samples.py --asset $1 --tf $2 --days $D --basis --out d2/${3}_basis_$half.npz 2>&1 | tail -1
  done; done
echo QUEUE2_DONE
