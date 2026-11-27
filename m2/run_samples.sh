#!/bin/bash
# M2-1 sampler driver: one capped job at a time; waits while a PF1 job runs or < 800 MB is available.
cd "$(dirname "$0")/.."
for v in "205 305" "330 430"; do
  set -- $v; M=$1; P=$2
  for mk in "btc 5" "btc 15" "eth 5"; do
    set -- $mk; A=$1; TF=$2
    for i in $(seq 14 23); do
      d=2026-11-$i; o=~/m2-out/s/$A$TF-$d-$M.npz
      [ -f $o ] && continue
      while pgrep -f '[p]f1' >/dev/null || [ $(free -m | awk '/Mem:/{print $7}') -lt 800 ]; do echo "waiting: busy box"; sleep 60; done
      M2_MATCH_MS=$M M2_PESS_MS=$P systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19 \
        ~/shadow-venv/bin/python m2/m2_samples.py --asset $A --tf $TF --days $d --final --out $o 2>&1 | tail -1
    done
  done
done
echo DONE
