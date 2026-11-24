#!/bin/bash
cd ~/dublin-1124
PY=$HOME/shadow-venv/bin/python
export PYTHONPATH=$HOME/overnight-wt:$HOME/dublin-retest/repo LVL_W=0xc387c2a40d389f17b723b6bba9b18b7dbd2de4f4
for tf in 5 15; do for d in 15 16 17 18 19 20 21 22 23; do
  p90=$(tail -1 ~/shadow-latency.csv | cut -d, -f5)
  if python3 -c "import sys;sys.exit(0 if float('$p90')>60 else 1)"; then echo "p90 $p90 > 60: stop"; exit 1; fi
  [ -s d1b/lvl/lvl-$tf-2026-11-$d.csv.gz ] && continue
  LVL_TF=$tf systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19 $PY $HOME/overnight-wt/dublin1124/mk_level_w.py 2026-11-$d 2>&1 | grep -E "wrote|BAD|Error|error|Killed" | tail -5
done; done
echo DONE
