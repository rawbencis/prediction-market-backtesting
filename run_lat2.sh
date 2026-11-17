#!/bin/bash
# Part 1: frozen F7i under dublin (regression, dsim only), live_lean, live_ack; days <= 2026-11-16; one job at a time; tag -v2
cd ~/overnight-wt
. ./guard.sh
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 PMLAT_OUTTAG=-v2"
job() { wait_ok; echo "start $1 $2 strict=$3 ackc=$4 $(date -u +%T) avail $(free -m | awk '/Mem/{print $7}')"
  $cap PMLAT_STRICT=$3 PMLAT_ACKC=$4 $HOME/shadow-venv/bin/python f7_altpull.py $2 $1 improve 2>&1 | tail -1; }
for d in 2026-11-14 2026-11-15 2026-11-16; do for a in 0 1; do job dublin $d 0 $a; done; done
for sc in live_lean live_ack; do for st in 0 1; do for a in 0 1; do for d in 2026-11-14 2026-11-15 2026-11-16; do job $sc $d $st $a; done; done; done; done
echo LAT2 DONE $(date -u +%T)
