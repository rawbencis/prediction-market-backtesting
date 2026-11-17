#!/bin/bash
# F8 holdout: frozen picks + F7i-B anchor, 2026-11-16 once, dsim then strict
cd ~/overnight-wt
. ./guard.sh
C=$(~/shadow-venv/bin/python -c "import json; print(';'.join(json.load(open('frozen/F8.json'))['picks'] + ['new|1|alt+out|5000|off']))")
cap="systemd-run --user --scope -q -p MemoryMax=800M -p MemorySwapMax=0 nice -n 10 env PYTHONPATH=$HOME/overnight-wt PMLAT_MAXDAY=2026-11-16 F8_TAG=-ho"
for st in 0 1; do wait_ok; echo "start strict=$st $(date -u +%T)"; $cap PMLAT_STRICT=$st "F8_CFGS=$C" $HOME/shadow-venv/bin/python f8_grid.py 2026-11-16 live_lean 2>&1 | tail -1; done
echo F8HO DONE $(date -u +%T)
