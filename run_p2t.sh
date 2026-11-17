#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
until grep -q "P3C DONE" out/run_p3c.log 2>/dev/null; do sleep 30; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16"
for scn in live dublin; do for d in 2026-11-14 2026-11-15 2026-11-16; do
  wait_ok; echo "start taker $scn $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p2_taker.py $d $scn 2>&1 | grep -E "wrote|Error|Traceback|Killed"
done; done
echo P2T DONE $(date -u +%T)
