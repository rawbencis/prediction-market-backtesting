#!/bin/bash
# F7i frozen picks under the live-measured latency scenarios; days <= 2026-11-16 only (PMLAT_MAXDAY), one job at a time
cd ~/overnight-wt
. ./guard.sh
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16"
for st in ${STS:-0 1}; do for sc in ${SCNS:-live_ack live_hold150}; do for d in 2026-11-14 2026-11-15 2026-11-16; do
  wait_ok; echo "start $sc $d strict=$st $(date -u +%T) avail $(free -m | awk '/Mem/{print $7}')"
  $cap PMLAT_STRICT=$st PMLAT_ACKC=${ACKC:-0} $HOME/shadow-venv/bin/python f7_altpull.py $d $sc improve 2>&1 | tail -1
done; done; done
echo LAT DONE $(date -u +%T)
