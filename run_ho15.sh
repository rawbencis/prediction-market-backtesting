#!/bin/bash
# 15m holdout: frozen F15 checks only, 2026-11-17 once (after the dev chain)
cd ~/overnight-wt
. ./guard.sh
until grep -q "P2T DONE" out/run_p2t.log 2>/dev/null; do sleep 30; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-17 PMLAT_STRICT=1"
for scn in live dublin; do
  wait_ok; echo "start ho F15-1 $scn $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python p2_f7i.py 2026-11-17 $scn alt15 2>&1 | tail -1
  wait_ok; echo "start ho F15-2 $scn $(date -u +%T)"; $cap P2_CFGS='bid|180000|out' $HOME/shadow-venv/bin/python p2_join.py 2026-11-17 $scn 2>&1 | tail -1
done
echo HO15 DONE $(date -u +%T)
