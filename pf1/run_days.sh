#!/bin/bash
# PF1: run run_day.py for each given day, one at a time, nice 15, MemoryMax 1200M; waits while available memory < 800 MB
# (gives up after 30 min). Usage: pf1/run_days.sh OUTDIR DAY...
set -u
OUT=$1; shift
mkdir -p "$OUT"
for d in "$@"; do
  for i in $(seq 1 180); do
    av=$(free -m | awk '/^Mem:/{print $7}')
    [ "$av" -ge 800 ] && break
    echo "$(date -u +%T) $d waiting: available ${av} MB" >&2; sleep 10
  done
  [ "$av" -lt 800 ] && { echo "$d stopped: available ${av} MB < 800" >&2; exit 2; }
  echo "$(date -u +%T) $d start, available ${av} MB" >&2
  systemd-run --user --scope -q -p MemoryMax=1200M nice -n 15 /usr/bin/time -f "$d maxrss %M KB %e s" \
    python3 -I "$(dirname "$0")/run_day.py" "$d" --out "$OUT" || { echo "$d failed" >&2; exit 1; }
done
