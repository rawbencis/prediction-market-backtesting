#!/bin/bash
# R-D3 (registered, BLIND): evaluate every finished day since 2026-11-15 that has no ~/rd3-res/rd3-<day>.csv yet.
# Waits for the nightly evals to be idle. Log = row counts only. Exit 2 if a day is not fully settled (unit retries in 1 h).
set -u
OUT=/home/ubuntu/rd3-res; LOG=$OUT/loop.log; mkdir -p $OUT
cd /home/ubuntu/dublin-retest/repo || exit 1
pending=0; d=2026-11-15; today=$(date -u +%F)
while [[ "$d" < "$today" ]]; do
  if [ ! -f $OUT/rd3-$d.csv ]; then
    for i in $(seq 1 60); do
      busy=0; for s in breakout-eval momentum-eval ohioai-match; do [ "$(systemctl is-active $s)" = active ] && busy=1; done
      [ $busy = 0 ] && awk "BEGIN{exit !($(cut -d' ' -f1 /proc/loadavg) < 1.5)}" && break; sleep 60
    done
    echo "$(date -u +%FT%T) run $d" >> $LOG
    /home/ubuntu/shadow-venv/bin/python rd3_eval.py --day $d 2>&1 | grep -E "wrote [0-9]+ rows|not writing|Error|Traceback|Killed" >> $LOG
    [ -f $OUT/rd3-$d.csv ] || pending=1
  fi
  d=$(date -u -d "$d + 1 day" +%F)
done
exit $((pending * 2))
