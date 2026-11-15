#!/bin/bash
# fav-hedge candidate B: re-pick over all 7 dev days (2026-11-14..20), same grid.json and pick rule as candidate A.
# Started by favhedge-repick.timer at 2026-11-21 00:30 UTC (memory cap from the unit). Waits until the nightly evals are
# idle (ohioai-match runs 00:45 for ~8 min) and the 1-min load is < 1.5. Holdout days (11-21..27) are not read.
set -u
LOG=/home/ubuntu/dublin-retest/fav-hedge/repick_B.log
cd /home/ubuntu/dublin-retest/repo || exit 1
DAYS="2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18 2026-11-19 2026-11-20"
T=$(date -u -d "2026-11-21 00:30" +%s)
echo "start $(date -u +%FT%T)" >> $LOG
for i in $(seq 1 120); do
  busy=0
  for s in breakout-eval momentum-eval ohioai-match; do [ "$(systemctl is-active $s)" = active ] && busy=1; done
  last=$(systemctl show ohioai-match -p ExecMainExitTimestamp --value)
  done_tonight=$([ -n "$last" ] && [ "$(date -u -d "$last" +%s)" -ge "$T" ] && echo 1 || echo 0)
  load=$(cut -d' ' -f1 /proc/loadavg)
  if [ $busy = 0 ] && [ "$done_tonight" = 1 ] && awk "BEGIN{exit !($load < 1.5)}"; then break; fi
  sleep 60
done
echo "go $(date -u +%FT%T) load $(cut -d' ' -f1 /proc/loadavg)" >> $LOG
PY="nice -n 19 /home/ubuntu/shadow-venv/bin/python"
for d in $DAYS; do $PY dsim.py winners --day $d >> $LOG 2>&1; done
$PY fav_hedge.py stage1 --days $DAYS --out-prefix stage1B >> $LOG 2>&1 &&
$PY fav_hedge.py freeze stage1B-btc5m-summary.csv frozen_B.json B $DAYS >> $LOG 2>&1
rc=$?
echo "end $(date -u +%FT%T) rc=$rc" >> $LOG
exit $rc
