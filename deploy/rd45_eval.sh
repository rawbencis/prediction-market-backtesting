#!/bin/bash
# R-D4/R-D5 nightly recorded-data shadow (NOT INSTALLED; operator decides). Evaluates every finished day since 2026-11-17 that has no
# out/pmlat/f7i-dublin-<day>-strict.csv.gz yet, one job at a time, MemoryMax 800M, after the other nightly evals are idle; then writes the report.
set -u
cd /home/ubuntu/overnight-wt || exit 1
. ./guard.sh 2>/dev/null || true
PY=/home/ubuntu/shadow-venv/bin/python
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=/home/ubuntu/overnight-wt"
d=2026-11-17; today=$(date -u +%F)
while [[ "$d" < "$today" ]]; do
  for s in breakout-eval momentum-eval rd3-eval ohioai-match; do while [ "$(systemctl is-active $s)" = active ]; do sleep 60; done; done
  [ -f out/pmlat/f7i-dublin-$d.csv.gz ] || $cap $PY f7_altpull.py $d dublin improve
  [ -f out/pmlat/f7i-dublin-$d-strict.csv.gz ] || $cap env PMLAT_STRICT=1 $PY f7_altpull.py $d dublin improve
  d=$(date -u -d "$d + 1 day" +%F)
done
{ $PY rd45_report.py --since 2026-11-17; echo; $PY rd45_report.py --since 2026-11-17 --strict; } > out/pmlat/rd45-report.txt 2>&1
