#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
while pgrep -f "run_queue[234].sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_SIZE=25 $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do wait_ok; $cap f7_altpull.py $d dublin improve 2>&1 | tail -1; done
echo QUEUE5 DONE
