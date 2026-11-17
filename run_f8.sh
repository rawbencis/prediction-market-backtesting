#!/bin/bash
# F8 grid on dev under live_lean, after the part-1 queue; one job at a time
cd ~/overnight-wt
. ./guard.sh
while pgrep -f run_lat2.sh >/dev/null; do sleep 20; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16"
wait_ok; echo "test $(date -u +%T)"; /usr/bin/time -f "test %e s %M KB" $cap F8_TAG=-test $HOME/shadow-venv/bin/python f8_grid.py 2026-11-14 live_lean 10 2>&1 | tail -3
for d in 2026-11-14 2026-11-15; do wait_ok; echo "start $d $(date -u +%T) avail $(free -m | awk '/Mem/{print $7}')"
  $cap $HOME/shadow-venv/bin/python f8_grid.py $d live_lean 2>&1 | tail -1; done
echo F8DEV DONE $(date -u +%T)
