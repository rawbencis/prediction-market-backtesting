#!/bin/bash
# PF1: extract Polymarket BTC 15m books for the given days, one day at a time, nice 15, 1.2 GB cap; waits if < 800 MB available.
cd "$(dirname "$0")/.."
for d in "$@"; do
  while [ "$(free -m | awk '/^Mem:/{print $7}')" -lt 800 ]; do echo "waiting: <800 MB available"; sleep 60; done
  [ -f ~/pf1-cache/$d/.done ] && continue
  systemd-run --user --scope -q -p MemoryMax=1200M -p MemorySwapMax=0 nice -n 15 ~/shadow-venv/bin/python -I pf1/extract_pm.py "$d" 2>&1 | grep -E "total|Error|Traceback" && [ -f ~/pf1-cache/$d/pm-00.npz ] && touch ~/pf1-cache/$d/.done
done
