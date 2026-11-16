#!/bin/bash
cd ~/overnight-wt
for mode in $MODES; do for sc in $SCN; do for d in $DAYS; do
  systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 ~/shadow-venv/bin/python f3_maker.py $d $sc btc 5 all $mode 2>&1 | tail -1
done; done; done
echo F3B DONE
