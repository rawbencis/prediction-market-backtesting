#!/bin/bash
# F9 item 8 dev pass (after run_f9dev.sh), one job at a time, 800 MB cap
cd ~/overnight-wt
. ./guard.sh
until grep -q "F9DEV DONE" out/run_f9dev.log; do sleep 30; done
C=$(~/shadow-venv/bin/python -c "import f9_grid; print(';'.join(f9_grid.ITEM8))" 2>/dev/null)
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 F9_TAG=-i8"
for d in 2026-11-14 2026-11-15; do
  wait_ok; echo "start i8 $d $(date -u +%T)"
  $cap $HOME/shadow-venv/bin/python f9_grid.py $d live 5 "$C" 2>&1 | tail -1
done
echo F9I8 DONE $(date -u +%T)
