#!/bin/bash
cd ~/overnight-wt
for sc in $SCN; do for d in $DAYS; do
  systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 ~/shadow-venv/bin/python f3_maker.py $d $sc ${ASSET:-btc} ${TF:-5} 2>&1 | tail -1
done; done
