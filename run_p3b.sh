#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
until grep -q "P3 DONE" out/run_p3.log 2>/dev/null; do sleep 30; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 PMLAT_STRICT=1 F9_TAG=-strict"
for x in "live 2026-11-14" "live 2026-11-15" "live 2026-11-16" "dublin 2026-11-14" "dublin 2026-11-15" "dublin 2026-11-16"; do
  set -- $x; wait_ok; echo "start alt15 $1 $2 $(date -u +%T)"; $cap $HOME/shadow-venv/bin/python f9_grid.py $2 $1 15 2>&1 | tail -1
done
echo P3B DONE $(date -u +%T)
