#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
until grep -q "P2 DONE" out/run_p2.log; do sleep 30; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 PMLAT_STRICT=1"
for src in alt15 b5a5; do for scn in live dublin; do for d in 2026-11-14 2026-11-15 2026-11-16; do
  wait_ok; echo "start f7i15 $src $scn $d $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p2_f7i.py $d $scn $src 2>&1 | tail -1
done; done; done
echo P2F7 DONE $(date -u +%T)
