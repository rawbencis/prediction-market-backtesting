#!/bin/bash
cd ~/overnight-wt
while pgrep -f "run_f3.sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do $cap f4_cross.py $d dublin 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f5_imbalance.py $d dublin 2>&1 | tail -1; done
$cap f6_bursts.py 2026-11-14 2026-11-15 > out/pmlat/f6-dev.txt 2>&1; echo "f6 rc=$?"
echo QUEUE1 DONE
