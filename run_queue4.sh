#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
while pgrep -f "run_queue[23].sh" >/dev/null; do sleep 10; done
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for d in 2026-11-14 2026-11-15; do wait_ok; $cap f7_altpull.py $d dublin_p90 improve 2>&1 | tail -1; done
for d in 2026-11-14 2026-11-15; do wait_ok; $cap f3_maker.py $d dublin_p90 btc 5 all improve 2>&1 | tail -1; done
for sc in dublin tokyo; do for d in 2026-11-14 2026-11-15; do wait_ok; $cap f7_altpull.py $d $sc improve btc 15 2>&1 | tail -1; done; done
echo QUEUE4 DONE
