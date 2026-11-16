#!/bin/bash
cd ~/overnight-wt
while pgrep -f "run_f7.sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do $cap f3_maker.py $d tokyo btc 5 all improve 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f6_sim.py $d tokyo 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f4_cross.py $d tokyo 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f1_sim.py $d tokyo 2>&1 | tail -1; done
echo QUEUE2 DONE
