#!/usr/bin/env bash
# Cross-asset opposite-direction pair study, one shot. Every stage is independent and fail-soft.
#   nohup bash run_xpair.sh > xpair.log 2>&1 &   then   tail -f xpair.log
# Env overrides: PY CACHE DATA OUT WORKERS ASSETS KSTART KEND
set -u
cd "$(dirname "$0")"
PY=${PY:-python3}
CACHE=${CACHE:-/home/ubuntu/stwf2-cache}
DATA=${DATA:-/home/ubuntu/up_down_research/data}
OUT=${OUT:-output/xpair}
W=${WORKERS:-4}
ASSETS=${ASSETS:-btc,eth,sol,xrp}
OTHERS=$(echo "$ASSETS" | sed 's/^btc,\?//')
step() { echo; echo "#### $* ($(date -u +%H:%M:%S) UTC)"; }

step "0 tests"
if ! $PY -W ignore -m unittest -q test_xasset_pair > /tmp/xpair_tests.log 2>&1; then
  tail -30 /tmp/xpair_tests.log; echo "#### TESTS FAILED - stopping"; exit 1
fi
tail -3 /tmp/xpair_tests.log
step "1 inventory";            $PY xasset_pair.py inv --data "$DATA" --btc-cache "$CACHE" --out "$OUT" || echo "!! inv failed"
step "2 settlements $OTHERS";  $PY xasset_pair.py settle --btc-cache "$CACHE" --out "$OUT" --assets "$OTHERS" || echo "!! settle failed"
step "3 Binance 1s klines";    $PY xasset_pair.py klines --out "$OUT" --assets "$ASSETS" --start "${KSTART:-2026-04}" --end "${KEND:-2026-07}" || echo "!! klines failed"
step "4 quote grids";          $PY xasset_pair.py grid --out "$OUT" --assets "$ASSETS" --npz "btc=$CACHE" --workers "$W" | tail -5 || echo "!! grid failed"
step "5 pair scans";           $PY xasset_pair.py pairs --out "$OUT" --btc-cache "$CACHE" --workers "$W" | tail -5 || echo "!! pairs failed"
step "6 report";               $PY -W ignore xasset_pair.py report --out "$OUT" --btc-cache "$CACHE" --assets "$ASSETS" || echo "!! report failed"
echo; echo "#### DONE ($(date -u +%H:%M:%S) UTC) - report in $OUT/report.txt"
