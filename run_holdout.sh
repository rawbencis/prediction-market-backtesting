#!/bin/bash
# ONE holdout run of the frozen picks on 2026-11-16 (BTC 5m). Waits for >= 01:10 UTC, idle nightly evals, other queues done.
cd ~/overnight-wt
. ./guard.sh
D=2026-11-16
T=$(date -u -d "2026-11-17 01:10" +%s)
while :; do
  now=$(date -u +%s); busy=0
  for s in breakout-eval momentum-eval rd3-eval ohioai-match; do [ "$(systemctl is-active $s)" = active ] && busy=1; done
  pgrep -f "run_queue[2345678].sh" >/dev/null && busy=1
  [ $now -ge $T ] && [ $busy = 0 ] && break
  sleep 30
done
echo "holdout start $(date -u +%FT%T) load $(cut -d' ' -f1 /proc/loadavg) mem_avail $(free -m | awk '/Mem/{print $7}')"
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt $HOME/shadow-venv/bin/python"
wait_ok; $cap f1_events.py $D 2>&1 | tail -1
for sc in dublin tokyo dublin_p90; do wait_ok; $cap f3_maker.py $D $sc btc 5 all improve 2>&1 | tail -1; done
for sc in dublin tokyo dublin_p90; do wait_ok; $cap f7_altpull.py $D $sc improve 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; $cap f6_sim.py $D $sc 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; $cap f4_cross.py $D $sc 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; $cap f1_sim.py $D $sc 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; env PMLAT_STRICT=1 $cap f7_altpull.py $D $sc improve 2>&1 | tail -1; done
for sc in dublin tokyo; do wait_ok; env PMLAT_STRICT=1 $cap f3_maker.py $D $sc btc 5 all improve 2>&1 | tail -1; done
wait_ok; $cap holdout_eval.py $D > out/pmlat/holdout-$D.txt 2>&1
wait_ok; env PMLAT_STRICT=1 $cap holdout_eval.py $D > out/pmlat/holdout-$D-strict.txt 2>&1
echo "holdout end $(date -u +%FT%T)"
