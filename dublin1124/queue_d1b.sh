#!/bin/bash
cd ~/dublin-1124
PY=$HOME/shadow-venv/bin/python
W=0xc387c2a40d389f17b723b6bba9b18b7dbd2de4f4
for spec in "btc 5" "btc 15" "eth 5"; do set -- $spec
 for d in 14 15 16 17 18 19 20 21 22 23; do
  p90=$(tail -1 ~/shadow-latency.csv | cut -d, -f5)
  if python3 -c "import sys;sys.exit(0 if float('$p90')>60 else 1)"; then echo "p90 $p90 > 60: stop"; exit 1; fi
  o=d1b/c387_$1_$2_2026-11-$d.pkl
  [ -s $o ] && continue
  systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19 $PY d1_rows.py $W $1 $2 2026-11-$d $o 2>&1 | grep -v "^ " | tail -2
 done
done
echo DONE
