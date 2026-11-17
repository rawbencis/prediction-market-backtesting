#!/bin/bash
cd ~/overnight-wt
. ./guard.sh
until grep -q "P3B DONE" out/run_p3b.log 2>/dev/null; do sleep 30; done
wait_ok; echo "start census $(date -u +%T)"
systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% -p MemorySwapMax=0 nice -n 19 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 $HOME/shadow-venv/bin/python p3_census.py 2026-11-14 2026-11-15 2026-11-16 > out/p3_census.md 2>&1
echo P3C DONE $(date -u +%T)
