#!/bin/bash
# Task 2 loop as amended 00:10 UTC: until 07:00 UTC (epoch-based, works across midnight), `|| true`, ~15 cycles/hour.
# Stops on a fill, CLEANUP FAILED, or an unconfirmed cancel.
END=$(date -u -d "$(date -u +%Y-%m-%d) 07:00" +%s); [ $(date -u +%s) -ge $END ] && END=$((END + 86400))
LOG=~/dublin-retest/out/probe-hourly.log
while [ $(date -u +%s) -lt $END ]; do
  echo "=== run $(date -u +%FT%T)" >> $LOG
  OUT=$(~/shadow-venv/bin/python ~/order_probe.py --env ~/probe.env --cycles 15 --gap 10 2>&1 || true)
  echo "$OUT" >> $LOG
  if echo "$OUT" | grep -qE "FILL on probe|CLEANUP FAILED|not confirmed cancelled|'matched': [1-9]"; then
    echo "=== STOPPED by loop guard $(date -u +%FT%T)" >> $LOG; exit 1
  fi
  sleep 2400
done
echo "=== loop end $(date -u +%FT%T)" >> $LOG
