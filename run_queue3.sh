#!/bin/bash
cd ~/overnight-wt
while pgrep -f "run_queue2.sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do $cap f7_altpull.py $d tokyo improve 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f7_altpull.py $d dublin join 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do $cap f7_altpull.py $d dublin_p90 improve 2>&1 | tail -1; done
echo QUEUE3 DONE
