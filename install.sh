#!/usr/bin/env bash
set -Eeuo pipefail
if (( EUID != 0 )); then echo 'Run: sudo bash install.sh' >&2; exit 1; fi
if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
  echo 'Requires Ubuntu with apt and systemd.' >&2; exit 1
fi
if [[ ! -r /etc/os-release ]]; then echo 'Cannot detect OS.' >&2; exit 1; fi
. /etc/os-release
if [[ "${ID:-}" != ubuntu ]]; then echo "Ubuntu required; found ${ID:-unknown}" >&2; exit 1; fi
case "${VERSION_ID:-}" in
  22.04|22.10|23.04|23.10|24.04|24.10|25.04|25.10|26.04) ;;
  *) echo "Unsupported/unverified Ubuntu version: ${VERSION_ID:-unknown}; supported target is Ubuntu 22.04 through 26.04." >&2; exit 1 ;;
esac
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SOURCE_DIR/routepilot.py" || ! -f "$SOURCE_DIR/uninstall.sh" || ! -f "$SOURCE_DIR/repair-user-launcher.sh" ]]; then echo 'Missing routepilot.py or uninstall.sh.' >&2; exit 1; fi
for asset in routepilot.svg routepilot.png; do
  [[ -s "$SOURCE_DIR/assets/$asset" ]] || { echo "Missing icon: $asset" >&2; exit 1; }
done
python3 -m py_compile "$SOURCE_DIR/routepilot.py" || exit 1
# policykit-1 is a transitional package on older releases and absent on newer ones.
# Both component packages are directly available on supported Ubuntu versions.
REQUIRED=(python3 python3-tk iproute2 polkitd pkexec network-manager nftables)
echo "Installing RoutePilot on Ubuntu ${VERSION_ID}..."
apt-get update
apt-get install -y "${REQUIRED[@]}"
for exe in python3 ip nft pkexec systemctl; do
  command -v "$exe" >/dev/null || { echo "Missing executable after install: $exe" >&2; exit 1; }
done
python3 - <<'PYTEST'
import tkinter, json, ipaddress
PYTEST
# Validate sources and dependencies BEFORE interrupting any running service.
python3 -m py_compile "$SOURCE_DIR/routepilot.py"
install -d -m 755 /opt/routepilot /etc/routepilot /var/lib/routepilot
install -m 755 "$SOURCE_DIR/routepilot.py" /opt/routepilot/routepilot.py
install -m 755 "$SOURCE_DIR/uninstall.sh" /opt/routepilot/uninstall.sh
install -m 755 "$SOURCE_DIR/repair-user-launcher.sh" /opt/routepilot/repair-user-launcher.sh
install -m 644 "$SOURCE_DIR/assets/routepilot.png" /opt/routepilot/routepilot.png
install -m 644 "$SOURCE_DIR/assets/routepilot.svg" /opt/routepilot/routepilot.svg
install -d -m 755 /usr/share/icons/hicolor/scalable/apps /usr/share/icons/hicolor/256x256/apps
install -m 644 "$SOURCE_DIR/assets/routepilot.svg" /usr/share/icons/hicolor/scalable/apps/routepilot.svg
install -m 644 "$SOURCE_DIR/assets/routepilot.png" /usr/share/icons/hicolor/256x256/apps/routepilot.png
if [[ ! -e /etc/routepilot/rules.json ]]; then
  (umask 022; printf '{"rules": []}\n' > /etc/routepilot/rules.json)
fi
cat > /etc/systemd/system/routepilot.service <<'EOF'
[Unit]
Description=RoutePilot persistent destination routing
Wants=network-online.target
After=network-online.target NetworkManager.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/routepilot/routepilot.py daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
install -d -m 755 /usr/share/applications
cat > /usr/share/applications/routepilot-manager.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=RoutePilot
Icon=/opt/routepilot/routepilot.png
StartupWMClass=RoutePilot
Comment=Manage persistent IP/CIDR/FQDN/Wildcard routing rules
Exec=/usr/bin/python3 /opt/routepilot/routepilot.py gui
Terminal=false
Categories=Network;
EOF
# Remove only a known obsolete system-wide legacy launcher.
# User-local launchers are not removed or overwritten.
# Clean up only an outdated legacy launcher with that exact name (if present).
# Back up legacy system launchers, leaving exactly one system desktop entry.
for legacy in /usr/share/applications/routepilot.desktop /usr/local/share/applications/routepilot.desktop; do
  if [[ -f "$legacy" ]] && grep -q '^Exec=/usr/bin/python3 /opt/routepilot/routepilot.py gui$' "$legacy"; then
    install -d -m 755 /opt/routepilot/launcher-backups
    cp -p "$legacy" "/opt/routepilot/launcher-backups/$(basename "$legacy").old"
    rm -f -- "$legacy"
  fi
done
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate /usr/share/applications/routepilot-manager.desktop
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
systemctl daemon-reload
systemctl enable routepilot.service
systemctl restart routepilot.service
if ! systemctl is-active --quiet routepilot.service; then
  echo 'RoutePilot service did not start. Inspect: sudo journalctl -u routepilot -n 100 --no-pager' >&2
  exit 1
fi
echo 'Installed. Close all running RoutePilot windows, then reopen it from Applications.'
echo 'Desktop icon: routepilot (hicolor theme); launcher: /usr/share/applications/routepilot-manager.desktop'
echo 'Wildcard uses passive UDP/53 DNS observation; VPN/Tailscale DNS settings are unchanged.'
echo 'If the old icon remains, run without sudo: bash repair-user-launcher.sh --check'
echo 'To migrate legacy user launchers: bash repair-user-launcher.sh --fix'

