#!/usr/bin/env bash
# Remove the Poseidon Vistek LCD driver and its boot integration.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "run with sudo: sudo ./uninstall.sh"; exit 1; fi

systemctl disable --now vistek-display.service 2>/dev/null || true
rm -f /etc/systemd/system/vistek-display.service
systemctl daemon-reload

rm -f /usr/local/bin/vistek.py
rm -f /etc/modules-load.d/vistek-nct6687.conf
rm -f /etc/modprobe.d/vistek-nct6687.conf
rm -f /etc/udev/rules.d/99-vistek-display.rules
udevadm control --reload-rules 2>/dev/null || true

echo "Removed. (kept /etc/default/vistek-display; delete it manually if you want.)"
echo "The nct6687 module stays loaded until reboot; 'sudo modprobe -r nct6683' to unload now."
