#!/bin/bash
# Re-run the shadow evaluator over the T1 holdout days (11-13..22; 11-23 done by hand) into ~/lp-arb/shadow-test,
# one day at a time under the heavy-job cap. Used once to check reproduction and to get the per-trigger
# persistence on the holdout (descriptive, defined in frozen/t1_shadow_eval_spec.json before this ran).
cd /home/ubuntu/lighter-flips
for d in $(seq 13 22); do
  systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 python3 lp_arb/shadow_eval.py --day 2026-11-$d --out ~/lp-arb/shadow-test
done
echo HOLDOUTCHECKDONE
