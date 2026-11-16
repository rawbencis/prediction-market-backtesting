#!/bin/bash
# run f1_sim for given days and scenarios, one job at a time, memory-capped
cd ~/overnight-wt
for sc in $SCN; do for d in $DAYS; do
  systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 ~/shadow-venv/bin/python f1_sim.py $d $sc ${ASSET:-btc} ${TF:-5} 2>&1 | tail -1
done; done
