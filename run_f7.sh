#!/bin/bash
cd ~/overnight-wt
for mode in $MODES; do for d in $DAYS; do
systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 ~/shadow-venv/bin/python f7_altpull.py $d ${SCN:-dublin} $mode 2>&1 | tail -1
done; done; echo F7 DONE
