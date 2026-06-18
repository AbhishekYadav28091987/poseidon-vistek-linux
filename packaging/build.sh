#!/usr/bin/env bash
# Build the vistek-display .deb and a generic .tar.gz from this repo.
# Usage: packaging/build.sh   ->  outputs to dist/
set -euo pipefail

VERSION="${VERSION:-1.0.0}"
PKG="vistek-display"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="$REPO/dist"
STAGE="$REPO/build/${PKG}-${VERSION}"

rm -rf "$REPO/build" "$STAGE"
mkdir -p "$OUT" "$STAGE"

# ----------------------------------------------------------- file tree (payload)
install -D -m0644 "$REPO/vistek.py"         "$STAGE/usr/lib/vistek/vistek.py"
install -D -m0644 "$REPO/vistek_widget.py"  "$STAGE/usr/lib/vistek/vistek_widget.py"
install -D -m0644 "$HERE/icon/vistek.svg"   "$STAGE/usr/share/icons/hicolor/scalable/apps/vistek.svg"
install -D -m0644 "$HERE/vistek-widget.desktop" "$STAGE/usr/share/applications/vistek-widget.desktop"
# autostart the widget on login (so it appears as a desktop widget)
install -D -m0644 "$HERE/vistek-widget.desktop" "$STAGE/etc/xdg/autostart/vistek-widget.desktop"
printf 'X-GNOME-Autostart-enabled=true\n' >> "$STAGE/etc/xdg/autostart/vistek-widget.desktop"

# CLI wrappers
install -d "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/vistek" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/vistek/vistek.py "$@"
EOF
cat > "$STAGE/usr/bin/vistek-widget" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/vistek/vistek_widget.py "$@"
EOF
chmod 0755 "$STAGE/usr/bin/vistek" "$STAGE/usr/bin/vistek-widget"

# systemd service
install -D -m0644 /dev/stdin "$STAGE/lib/systemd/system/vistek-display.service" <<'EOF'
[Unit]
Description=COUGAR Poseidon Vistek LCD - CPU stats display
After=multi-user.target

[Service]
Type=simple
EnvironmentFile=-/etc/default/vistek-display
ExecStart=/usr/bin/vistek daemon
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF

# tmpfiles (creates /run/vistek for the status file)
install -D -m0644 /dev/stdin "$STAGE/usr/lib/tmpfiles.d/vistek.conf" <<'EOF'
d /run/vistek 0755 root root -
EOF

# boot integration + config (conffiles)
install -D -m0644 /dev/stdin "$STAGE/etc/modules-load.d/vistek-nct6687.conf" <<'EOF'
nct6683
EOF
install -D -m0644 /dev/stdin "$STAGE/etc/modprobe.d/vistek-nct6687.conf" <<'EOF'
options nct6683 force=1
EOF
install -D -m0644 /dev/stdin "$STAGE/etc/udev/rules.d/99-vistek-display.rules" <<'EOF'
# COUGAR Poseidon Vistek ARGB 1.9" display
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2c65", ATTRS{idProduct}=="1000", MODE="0660", TAG+="uaccess"
EOF
install -D -m0644 /dev/stdin "$STAGE/etc/default/vistek-display" <<'EOF'
# Update interval in seconds (Windows uses 0.5; panel needs a steady feed)
VISTEK_INTERVAL=0.5
# Single RPM shown on the LCD; comma list = cycle every VISTEK_ALT_SECS seconds
VISTEK_FAN_CH=1,16
VISTEK_ALT_SECS=5
VISTEK_PUMP_CH=1
# Wait for the USB display at startup (boot ordering)
VISTEK_WAIT=1
EOF

# ----------------------------------------------------------------- post steps
read -r -d '' POST <<'EOF' || true
modprobe nct6683 force=1 2>/dev/null || true
systemd-tmpfiles --create /usr/lib/tmpfiles.d/vistek.conf 2>/dev/null || mkdir -p /run/vistek
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl enable --now vistek-display.service 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q 2>/dev/null || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
EOF

# =========================================================== build the .deb
build_deb() {
  local D="$REPO/build/deb"
  rm -rf "$D"; mkdir -p "$D"
  cp -a "$STAGE/." "$D/"
  mkdir -p "$D/DEBIAN"
  cat > "$D/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.6), python3-tk
Recommends: kmod
Maintainer: Vistek Project <noreply@localhost>
Description: COUGAR Poseidon Vistek AIO cooler LCD driver and desktop widget
 Drives the USB LCD on the COUGAR Poseidon Vistek ARGB liquid cooler
 (USB 2c65:1000), showing CPU temperature, load, clock, power and fan
 speeds on the screen, and provides a desktop widget with animated fans
 that mirrors the same live data.
EOF
  cat > "$D/DEBIAN/conffiles" <<EOF
/etc/default/vistek-display
/etc/modprobe.d/vistek-nct6687.conf
/etc/modules-load.d/vistek-nct6687.conf
/etc/udev/rules.d/99-vistek-display.rules
EOF
  { echo '#!/bin/sh'; echo 'set -e'; echo "$POST"; echo 'exit 0'; } > "$D/DEBIAN/postinst"
  cat > "$D/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
  systemctl disable --now vistek-display.service 2>/dev/null || true
fi
exit 0
EOF
  cat > "$D/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
systemctl daemon-reload 2>/dev/null || true
exit 0
EOF
  chmod 0755 "$D/DEBIAN/postinst" "$D/DEBIAN/prerm" "$D/DEBIAN/postrm"
  dpkg-deb --root-owner-group --build "$D" "$OUT/${PKG}_${VERSION}_all.deb" >/dev/null
  echo "built: $OUT/${PKG}_${VERSION}_all.deb"
}

# ===================================================== build the generic tarball
build_tar() {
  local T="$REPO/build/tar/${PKG}-${VERSION}"
  rm -rf "$REPO/build/tar"; mkdir -p "$T/payload"
  cp -a "$STAGE/." "$T/payload/"
  # generic installer for non-deb distros
  cat > "$T/install.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
[ "\$EUID" -eq 0 ] || { echo "run with sudo: sudo ./install.sh"; exit 1; }
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
cp -a "\$DIR/payload/." /
$POST
echo
echo "Installed. Service: systemctl status vistek-display"
echo "Launch the widget from your app menu (Poseidon Vistek Monitor) or: vistek-widget"
EOF
  cat > "$T/uninstall.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run with sudo: sudo ./uninstall.sh"; exit 1; }
systemctl disable --now vistek-display.service 2>/dev/null || true
rm -f /usr/bin/vistek /usr/bin/vistek-widget
rm -rf /usr/lib/vistek
rm -f /usr/share/applications/vistek-widget.desktop /etc/xdg/autostart/vistek-widget.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/vistek.svg
rm -f /lib/systemd/system/vistek-display.service /usr/lib/tmpfiles.d/vistek.conf
rm -f /etc/modules-load.d/vistek-nct6687.conf /etc/modprobe.d/vistek-nct6687.conf
rm -f /etc/udev/rules.d/99-vistek-display.rules
systemctl daemon-reload 2>/dev/null || true
echo "Removed (kept /etc/default/vistek-display)."
EOF
  cp "$REPO/README.md" "$T/README.md" 2>/dev/null || true
  chmod 0755 "$T/install.sh" "$T/uninstall.sh"
  tar -C "$REPO/build/tar" -czf "$OUT/${PKG}-${VERSION}.tar.gz" "${PKG}-${VERSION}"
  echo "built: $OUT/${PKG}-${VERSION}.tar.gz"
}

build_deb
build_tar
echo "done."
