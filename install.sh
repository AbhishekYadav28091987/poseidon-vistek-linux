#!/usr/bin/env bash
# Installer for the Poseidon Vistek LCD driver on Linux.
# - installs vistek.py to /usr/local/bin
# - auto-loads the nct6687 fan sensor at boot (force mode)
# - installs + enables a systemd service that streams CPU temp/load/watt/RPM
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run with sudo: sudo ./install.sh"; exit 1; fi
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] install driver -> /usr/local/bin/vistek.py"
install -m 0755 "$SRC/vistek.py" /usr/local/bin/vistek.py

echo "[2/6] fan sensor (nct6687) autoload at boot"
echo nct6683 > /etc/modules-load.d/vistek-nct6687.conf
echo "options nct6683 force=1" > /etc/modprobe.d/vistek-nct6687.conf
modprobe nct6683 force=1 2>/dev/null || true   # load now if not already

echo "[3/6] udev rule (hidraw access for 2c65:1000)"
cat > /etc/udev/rules.d/99-vistek-display.rules <<'EOF'
# COUGAR Poseidon Vistek ARGB 1.9" display
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2c65", ATTRS{idProduct}=="1000", MODE="0660", TAG+="uaccess"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

echo "[4/6] config file -> /etc/default/vistek-display"
if [[ ! -f /etc/default/vistek-display ]]; then
cat > /etc/default/vistek-display <<'EOF'
# Update interval in seconds (Windows uses 0.5; the panel needs a steady feed)
VISTEK_INTERVAL=0.5
# Which nct6687 fan channel feeds the single RPM shown on the LCD.
# fan16 (~1230) and fan1 (~1460) are the two live channels on this board.
VISTEK_FAN_CH=16
VISTEK_PUMP_CH=1
# Block at startup until the USB display appears (good for boot ordering)
VISTEK_WAIT=1
EOF
fi

echo "[5/6] systemd service -> /etc/systemd/system/vistek-display.service"
cat > /etc/systemd/system/vistek-display.service <<'EOF'
[Unit]
Description=COUGAR Poseidon Vistek LCD - CPU stats display
After=multi-user.target

[Service]
Type=simple
EnvironmentFile=-/etc/default/vistek-display
ExecStart=/usr/bin/python3 /usr/local/bin/vistek.py daemon
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] enable + start service"
systemctl daemon-reload
systemctl enable --now vistek-display.service

echo
echo "Done. Status:  systemctl status vistek-display"
echo "Logs:          journalctl -u vistek-display -f"
echo "Tweak config:  sudo nano /etc/default/vistek-display  (then: systemctl restart vistek-display)"
