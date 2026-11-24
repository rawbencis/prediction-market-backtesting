#!/bin/bash
# usage: run_d3.sh "DAYS" ; skips done outputs; stops at the 00:12-01:10 blackout
cd ~/overnight-wt
for d in $1; do for a in btc eth sol xrp doge; do for tf in 5 15; do
  hm=$(date -u +%H%M); if [ $hm -ge 0011 ] && [ $hm -lt 0110 ]; then echo "blackout, stop at $hm"; exit 0; fi
  [ -f out/d3/taker-$a-$tf-$d.csv.gz ] && continue
  p90=$(tail -1 ~/shadow-latency.csv | cut -d, -f5); if python3 -c "import sys;sys.exit(0 if float('$p90')>60 else 1)"; then echo "p90 $p90 stop"; exit 1; fi
  systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19 env PYTHONPATH=$HOME/overnight-wt:$HOME/dublin-retest/repo $HOME/shadow-venv/bin/python dublin1124/d3_taker.py $d $a $tf 2>&1 | tail -1
done; done; done; echo D3_BATCH_DONE
