#!/bin/bash
# Second out-of-sample check: 2026-11-17 hours 00-04 (complete by 05:00), frozen F3i / F7i only. Starts >= 05:03 UTC after run_holdout.sh.
cd ~/overnight-wt
. ./guard.sh
D=2026-11-17
T=$(date -u -d "2026-11-17 05:03" +%s)
while [ $(date -u +%s) -lt $T ] || pgrep -f "run_holdout.sh" >/dev/null; do sleep 30; done
echo "holdout2 start $(date -u +%FT%T)"
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
for sc in dublin tokyo; do wait_ok; $cap f3_maker.py $D $sc btc 5 0,1,2,3,4 improve 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; $cap f7_altpull.py $D $sc improve btc 5 0,1,2,3,4 2>&1 | tail -1; done
$cap holdout_eval.py $D > out/pmlat/holdout-$D-h00-04.txt 2>&1
echo "holdout2 end $(date -u +%FT%T)"
