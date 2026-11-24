#!/bin/bash
# T1 holdout rerun with the fee takers actually paid (census mode); one day at a time, capped.
cd /home/ubuntu/lighter-flips/overnight
for d in $(seq 13 23); do
  systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 python3 t1_arb.py day --day 2026-11-$d --fee-mode census
done; echo T1CDONE
