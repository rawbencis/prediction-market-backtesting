#!/usr/bin/env bash
# Dublin (eu-west-1) recorder box, 2026-11-14. Fresh Ubuntu 24.04, user ubuntu. Run:
#   curl -fsSL https://raw.githubusercontent.com/rawbencis/prediction-market-backtesting/research/shadow-trades/deploy/dublin_setup.sh -o dublin_setup.sh
#   less dublin_setup.sh && bash dublin_setup.sh
# Installs: AWS time sync, repo (research/shadow-trades) in ~/lighter-flips, venv ~/shadow-venv,
# updown-rec (one socket per token, trade capture), shadow-latency (15 min), momentum-eval, ohioai-match timers.
# Read-only: no keys, no orders.
set -euo pipefail
DIR=/home/ubuntu/lighter-flips
VENV=/home/ubuntu/shadow-venv
PY=$VENV/bin/python

echo "== packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv python3-pip chrony tmux >/dev/null

echo "== clock: Amazon Time Sync"
echo "server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4" | sudo tee /etc/chrony/sources.d/aws.sources >/dev/null
sudo systemctl restart chrony
sleep 5

echo "== code + venv"
[ -d "$DIR/.git" ] || git clone -q -b research/shadow-trades --single-branch https://github.com/rawbencis/prediction-market-backtesting "$DIR"
cd "$DIR" && git pull -q
[ -x "$PY" ] || python3 -m venv "$VENV"
$PY -m pip install -q --upgrade pip
$PY -m pip install -q websockets orjson numpy pandas
$PY -m unittest -q test_latency_watch test_momentum_shadow test_shadow
mkdir -p /home/ubuntu/shadow-rec /home/ubuntu/mom-res /home/ubuntu/ohioai

echo "== services"
sudo tee /etc/systemd/system/updown-rec.service >/dev/null <<UNIT
[Unit]
Description=Up/down shadow recorder, Dublin (read-only, no keys)
After=network-online.target chrony.service
Wants=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$DIR
ExecStart=$PY shadow_rec.py rec --out /home/ubuntu/shadow-rec
Nice=-5
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
mk() {  # name, OnCalendar, command
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
mk shadow-latency "*-*-* *:00/15:30 UTC" "$PY latency_watch.py sample --rec /home/ubuntu/shadow-rec --log /home/ubuntu/shadow-latency.csv"
mk momentum-eval "*-*-* 00:25:00 UTC" "$PY momentum_shadow.py eval --rec /home/ubuntu/shadow-rec --res /home/ubuntu/mom-res && $PY momentum_shadow.py report --res /home/ubuntu/mom-res > /home/ubuntu/mom-res/report.txt 2>&1"
mk ohioai-match "*-*-* 00:45:00 UTC" "$PY ohioai_match.py --rec /home/ubuntu/shadow-rec --out /home/ubuntu/ohioai --days \$\$(date -u -d yesterday +%%F) > /home/ubuntu/ohioai/match-\$\$(date -u -d yesterday +%%F).txt 2>&1"
sudo systemctl daemon-reload
sudo systemctl enable --now updown-rec.service shadow-latency.timer momentum-eval.timer ohioai-match.timer

echo "== waiting 90 s for the recorder"
sleep 90
echo "== status"
systemctl is-active updown-rec
journalctl -u updown-rec --since "-2min" --no-pager | grep "rows pm=" | tail -2
chronyc tracking | grep -E "Reference ID|System time|Last offset"
systemctl list-timers --no-pager | grep -E "shadow-latency|momentum-eval|ohioai-match"
curl -s https://polymarket.com/api/geoblock; echo
