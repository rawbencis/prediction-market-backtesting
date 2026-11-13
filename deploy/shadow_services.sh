#!/usr/bin/env bash
# Shadow services, 2026-11-14. Run from ~/lighter-flips after merging research/shadow-trades:
#   bash deploy/shadow_services.sh
# 1) stops + disables n8n, n8n-job-runner, onetouch-shadow (re-enable: sudo systemctl enable --now <name>)
# 2) replaces updown-shadow with updown-rec (same recorder, same ~/shadow-rec, new code: trade capture on)
# 3) adds timers: shadow-latency (every 15 min), momentum-eval (00:25 UTC), ohioai-match (00:45 UTC)
set -euo pipefail
DIR=/home/ubuntu/lighter-flips
VENV=/home/ubuntu/shadow-venv/bin/python
cd "$DIR"
grep -q TR_REC shadow_rec.py || { echo "shadow_rec.py has no trade capture: merge research/shadow-trades first"; exit 1; }
for f in momentum_shadow.py ohioai_match.py latency_watch.py; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
$VENV -c "import websockets" || { echo "websockets missing in shadow-venv"; exit 1; }
python3 -c "import numpy, pandas" || { echo "system python3 needs numpy + pandas"; exit 1; }
$VENV -c "import orjson" 2>/dev/null || $VENV -m pip install -q orjson || echo "orjson not installed (optional)"

echo "== stopping n8n, n8n-job-runner, onetouch-shadow"
sudo systemctl disable --now n8n.service n8n-job-runner.service onetouch-shadow.service || true

echo "== recorder: updown-shadow -> updown-rec (trade capture on)"
sudo tee /etc/systemd/system/updown-rec.service >/dev/null <<UNIT
[Unit]
Description=Up/down shadow recorder with trade prints (read-only, no keys)
After=network-online.target
Wants=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$DIR
ExecStart=$VENV shadow_rec.py rec --out /home/ubuntu/shadow-rec
Nice=-5
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

mk() {  # name, OnCalendar, ExecStart
sudo tee /etc/systemd/system/$1.service >/dev/null <<UNIT
[Unit]
Description=$1 (shadow research job)
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$DIR
Nice=10
ExecStart=/bin/bash -c '$3'
UNIT
sudo tee /etc/systemd/system/$1.timer >/dev/null <<UNIT
[Unit]
Description=$1 timer
[Timer]
OnCalendar=$2
Persistent=true
[Install]
WantedBy=timers.target
UNIT
}
mk shadow-latency "*-*-* *:00/15:30 UTC" "python3 latency_watch.py sample --rec /home/ubuntu/shadow-rec --log /home/ubuntu/shadow-latency.csv"
mk momentum-eval "*-*-* 00:25:00 UTC" "python3 momentum_shadow.py eval --rec /home/ubuntu/shadow-rec --res /home/ubuntu/mom-res && python3 momentum_shadow.py report --res /home/ubuntu/mom-res > /home/ubuntu/mom-res/report.txt 2>&1"
mk ohioai-match "*-*-* 00:45:00 UTC" "python3 ohioai_match.py --rec /home/ubuntu/shadow-rec --out /home/ubuntu/ohioai --days \$\$(date -u -d yesterday +%%F) > /home/ubuntu/ohioai/match-\$\$(date -u -d yesterday +%%F).txt 2>&1"
mkdir -p /home/ubuntu/mom-res /home/ubuntu/ohioai

sudo systemctl daemon-reload
# swap recorders with no overlap (both would append to the same hourly files)
sudo systemctl disable --now updown-shadow.service
sudo systemctl enable --now updown-rec.service
sudo systemctl enable --now shadow-latency.timer momentum-eval.timer ohioai-match.timer

sleep 70
echo "== status"
systemctl is-active updown-rec.service n8n.service n8n-job-runner.service onetouch-shadow.service updown-shadow.service || true
journalctl -u updown-rec --since "-2min" --no-pager | grep -E "rows pm=" | tail -2
systemctl list-timers --no-pager | grep -E "shadow-latency|momentum-eval|ohioai-match"
echo "first latency sample:"; python3 latency_watch.py sample --rec /home/ubuntu/shadow-rec --log /home/ubuntu/shadow-latency.csv
