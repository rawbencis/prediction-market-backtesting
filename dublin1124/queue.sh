#!/bin/bash
# Heavy jobs after the 00:12-01:10 blackout, one at a time, recorder p90 checked before each.
J="systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19"
PY=$HOME/shadow-venv/bin/python
chk() { p90=$(tail -1 ~/shadow-latency.csv | cut -d, -f5); if python3 -c "import sys;sys.exit(0 if float('$p90')>60 else 1)"; then echo "p90 $p90 > 60: stop"; exit 1; fi; while pgrep -f "python.*(d3_taker|d2_samples|alt_bid|p2_join|d4_atlas)" >/dev/null; do sleep 5; done; }
true
echo "QUEUE START $(date -u +%H:%M:%S)"
HO="2026-11-19 2026-11-20 2026-11-21 2026-11-22 2026-11-23"
cd ~/dublin-1124
for spec in "btc 5 btc5_ho" "btc 15 btc15_ho" "eth 5 eth5_ho" "btc 5 btc5_fin_ho --final" "btc 15 btc15_fin_ho --final" "eth 5 eth5_fin_ho --final" "eth 15 eth15_fin_ho --final"; do
  set -- $spec; chk; $J $PY d2_samples.py --asset $1 --tf $2 --days $HO --out d2/$3.npz $4 2>&1 | tail -1
done
echo "D2 HOLDOUT SAMPLES DONE $(date -u +%H:%M:%S)"
~/dublin-1124/run_d3.sh "2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18 $HO" | tail -2
echo "D3 TAKER DONE $(date -u +%H:%M:%S)"
cd ~/overnight-wt
for d in 2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18 $HO; do for a in eth sol xrp doge; do
  chk; $J env PMLAT_STRICT=1 PYTHONPATH=$HOME/overnight-wt:$HOME/dublin-retest/repo $PY alt_bid.py $d $a 2>&1 | grep wrote
done; done
echo "ALT BID DONE $(date -u +%H:%M:%S)"
for d in 2026-11-14 2026-11-15 2026-11-16 2026-11-17 2026-11-18 $HO; do for tf in 5 15; do
  chk; $J env PMLAT_STRICT=1 P2_CFGS="bid|60000|none" P2_TAG=-d5 PYTHONPATH=$HOME/overnight-wt:$HOME/dublin-retest/repo $PY p2_join.py $d live btc $tf 2>&1 | tail -1
done; done
echo "D5 JOIN DONE $(date -u +%H:%M:%S)"
chk; $J env PYTHONPATH=$HOME/overnight-wt:$HOME/dublin-retest/repo $PY dublin1124/d4_atlas.py 2026-11-22 2026-11-23 > ~/dublin-1124/d4.md 2> ~/dublin-1124/d4.err
echo "D4 DONE $(date -u +%H:%M:%S)"
echo QUEUE_ALL_DONE
