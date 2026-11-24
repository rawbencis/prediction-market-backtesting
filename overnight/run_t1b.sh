#!/bin/bash
cd /home/ubuntu/lighter-flips/overnight
for mode in inforce frozen07; do for d in $(seq 13 23); do
  systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=80% nice -n 19 python3 t1_arb.py day --day 2026-11-$d --fee-mode $mode
done; done; echo T1BDONE
