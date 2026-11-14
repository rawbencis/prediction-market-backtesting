#!/usr/bin/env bash
# Octopus signal-latency shadow (read-only), same on Tokyo and Dublin. Run from ~/lighter-flips:
#   bash deploy/sig_shadow_setup.sh
# Needs: ~/sig-wallets.json (same file on both boxes) and ~/.sig-shadow.env with ALCHEMY_API_KEY=... (chmod 600).
set -euo pipefail
DIR=/home/ubuntu/lighter-flips
PY=/home/ubuntu/shadow-venv/bin/python
cd "$DIR"
[ -f sig_shadow.py ] || { echo "sig_shadow.py missing: merge/pull research/shadow-trades first"; exit 1; }
[ -s /home/ubuntu/sig-wallets.json ] || { echo "missing ~/sig-wallets.json"; exit 1; }
grep -q '^ALCHEMY_API_KEY=.\+' /home/ubuntu/.sig-shadow.env || { echo "missing ALCHEMY_API_KEY in ~/.sig-shadow.env"; exit 1; }
chmod 600 /home/ubuntu/.sig-shadow.env
$PY -c "import websockets, numpy" 2>/dev/null || $PY -m pip install -q websockets numpy
$PY -c "import pandas" 2>/dev/null || $PY -m pip install -q pandas
$PY -m unittest -q test_sig_shadow
echo "wallets: $($PY sig_shadow.py wallets --wallets /home/ubuntu/sig-wallets.json | grep -c 0x)"
sudo tee /etc/systemd/system/sig-shadow.service >/dev/null <<UNIT
[Unit]
Description=Octopus signal-latency shadow (read-only: Alchemy logs + Polymarket RTDS activity)
After=network-online.target
Wants=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$DIR
EnvironmentFile=/home/ubuntu/.sig-shadow.env
ExecStart=$PY sig_shadow.py run --wallets /home/ubuntu/sig-wallets.json --out /home/ubuntu/sig-shadow
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now sig-shadow.service
echo "== waiting 130 s for two status lines"
sleep 130
systemctl is-active sig-shadow
journalctl -u sig-shadow --since "-3min" --no-pager | grep -E "watching|wallets=" | tail -3
