#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
until grep -q "P2F7 DONE" out/run_p2f7.log 2>/dev/null; do sleep 30; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 PMLAT_STRICT=1"
for d in 2026-11-15 2026-11-16; do wait_ok; echo "start sibling $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p3_sibling.py $d 2>&1 | grep -E "SUMMARY|Error|Traceback|Killed"; done
for scn in live dublin; do for d in 2026-11-14 2026-11-15 2026-11-16; do
  wait_ok; echo "start p3 $scn $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p3_grid.py $d $scn 2>&1 | tail -1
done; done
echo P3 DONE $(date -u +%T)
