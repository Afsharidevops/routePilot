#!/usr/bin/env bash
set -Eeuo pipefail
if (( EUID != 0 )); then echo 'Run: sudo bash uninstall.sh' >&2; exit 1; fi
PURGE=0
case "${1:-}" in
  '') ;;
  --purge-config) PURGE=1 ;;
  *) echo 'Usage: sudo bash uninstall.sh [--purge-config]' >&2; exit 2 ;;
esac
# Stop reconciler before removing routing state to prevent route re-creation.
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop routepilot.service 2>/dev/null || true
fi
if [[ -f /opt/routepilot/routepilot.py ]]; then
  echo 'Removing managed routes and firewall rules...'
  if ! /usr/bin/python3 /opt/routepilot/routepilot.py cleanup; then
    echo 'ERROR: Route cleanup failed. Installation was kept to allow recovery.' >&2
    echo 'Inspect: sudo journalctl -u routepilot -n 100 --no-pager' >&2
    exit 1
  fi
else
  echo 'WARNING: /opt/routepilot/routepilot.py is missing; cannot verify route cleanup.' >&2
  echo 'Refusing to remove the remaining files until managed routes are reviewed.' >&2
  exit 1
fi
systemctl disable routepilot.service 2>/dev/null || true
rm -f /etc/systemd/system/routepilot.service
rm -f /usr/share/applications/routepilot.desktop
rm -f /usr/share/applications/routepilot-manager.desktop
rm -f /usr/local/share/applications/routepilot.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/routepilot.svg
rm -f /usr/share/icons/hicolor/256x256/apps/routepilot.png
rm -rf /opt/routepilot
if (( PURGE )); then
  rm -rf /etc/routepilot /var/lib/routepilot
  echo 'Saved rules and RoutePilot state deleted (--purge-config).'
else
  echo 'Saved rules retained in /etc/routepilot/rules.json.'
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
systemctl daemon-reload
printf 'RoutePilot uninstalled successfully. Existing Pritunl/OpenVPN settings were not changed.\n'
