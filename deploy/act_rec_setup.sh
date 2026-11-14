#!/usr/bin/env bash
# Taker census: record every trade (with wallet) on the up/down markets the recorder covers.
# Read-only, no keys. Writes ~/shadow-rec/YYYY-MM-DD/act-HH.bin next to the books (same 14-day prune).
# Run from the repo root on each recorder box:  bash deploy/act_rec_setup.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV=/home/ubuntu/shadow-venv/bin/python
$VENV -c "import websockets" || { echo "websockets missing in shadow-venv"; exit 1; }
(cd "$DIR" && $VENV -m pytest -q -p no:cacheprovider test_act_rec.py 2>&1 | tail -1)
sudo tee /etc/systemd/system/updown-act.service >/dev/null <<UNIT
[Unit]
Description=Up/down taker census from Polymarket RTDS activity (read-only, no keys)
After=network-online.target
Wants=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$DIR
ExecStart=$VENV act_rec.py rec --out /home/ubuntu/shadow-rec
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now updown-act.service
sleep 70
systemctl is-active updown-act.service
journalctl -u updown-act.service --since "-2min" --no-pager | grep "act rows" | tail -1
ls -la /home/ubuntu/shadow-rec/$(date -u +%F)/act-*.bin | tail -2
