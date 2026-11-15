#!/bin/bash
# R-D3 daily evaluation (registered, BLIND). 00:40 UTC: for every finished day since 2026-11-15 without ~/rd3-res/rd3-<day>.csv,
# run rd3_eval.py memory-capped. The log holds row counts only (rd3_eval prints nothing else). Retries hourly while unsettled.
set -u
OUT=~/rd3-res; LOG=$OUT/loop.log; mkdir -p $OUT
cd ~/dublin-retest/repo || exit 1
run() {
  if systemd-run --user --scope -p MemoryMax=1200M -p MemorySwapMax=0 true >/dev/null 2>&1; then
    systemd-run --user --scope -p MemoryMax=1200M -p MemorySwapMax=0 nice -n 19 "$@"
  else
    bash -c 'ulimit -v 1500000; exec nice -n 19 "$@"' _ "$@"
  fi
}
while true; do
  now=$(date -u +%s); next=$(date -u -d "$(date -u +%F) 00:40" +%s); [ $now -ge $next ] && next=$((next + 86400))
  pending=0
  d=2026-11-15; today=$(date -u +%F)
  while [[ "$d" < "$today" ]]; do
    if [ ! -f $OUT/rd3-$d.csv ]; then
      for i in $(seq 1 60); do  # nightly evals idle and load < 1.5
        busy=0; for s in breakout-eval momentum-eval ohioai-match; do [ "$(systemctl is-active $s)" = active ] && busy=1; done
        [ $busy = 0 ] && awk "BEGIN{exit !($(cut -d' ' -f1 /proc/loadavg) < 1.5)}" && break; sleep 60
      done
      echo "$(date -u +%FT%T) run $d" >> $LOG
      run ~/shadow-venv/bin/python rd3_eval.py --day $d 2>&1 | grep -E "wrote [0-9]+ rows|not writing|Error|Traceback|Killed" >> $LOG
      [ -f $OUT/rd3-$d.csv ] || pending=1
    fi
    d=$(date -u -d "$d + 1 day" +%F)
  done
  if [ $pending = 1 ]; then sleep 3600; else sleep $((next - $(date -u +%s))); fi
done
