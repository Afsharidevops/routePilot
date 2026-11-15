#!/usr/bin/env bash
set -Eeuo pipefail
if (( EUID == 0 )); then echo 'Run as your normal desktop user (without sudo).' >&2; exit 1; fi
case "${1:---check}" in --check|--fix) MODE="${1:---check}";; *) echo 'Usage: bash repair-user-launcher.sh [--check|--fix]' >&2; exit 2;; esac
DESKTOP=/usr/share/applications/routepilot-manager.desktop
ICON=/opt/routepilot/routepilot.png
USER_APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LEGACY="$USER_APPS/routepilot.desktop"
NEW="$USER_APPS/routepilot-manager.desktop"
[[ -s "$DESKTOP" && -s "$ICON" ]] || { echo 'Missing installed launcher or icon. Reinstall first.' >&2; exit 1; }
echo "Current system launcher: $DESKTOP"
grep -E '^(Name|Icon|Exec|StartupWMClass)=' "$DESKTOP"
echo "Image: $(file -b "$ICON" 2>/dev/null || echo available)"
if [[ -f "$LEGACY" ]]; then
  echo "Legacy user launcher: $LEGACY"
  if grep -q '^Exec=/usr/bin/python3 /opt/routepilot/routepilot.py gui$' "$LEGACY"; then
    if [[ "$MODE" == --fix ]]; then
      stamp="$(date +%Y%m%d-%H%M%S)-$$"
      mv -- "$LEGACY" "$LEGACY.backup-$stamp"
      echo "Moved old launcher to $LEGACY.backup-$stamp"
    fi
  else
    echo 'Legacy file is not recognized as RoutePilot; leaving it untouched.'
  fi
fi
if [[ -f "$NEW" ]]; then
  echo "Existing modern user launcher: $NEW"
  if [[ "$MODE" == --fix ]] && grep -q '^Exec=/usr/bin/python3 /opt/routepilot/routepilot.py gui$' "$NEW"; then
    stamp="$(date +%Y%m%d-%H%M%S)-$$"
    mv -- "$NEW" "$NEW.backup-$stamp"
    echo "Moved duplicate user launcher to $NEW.backup-$stamp"
  fi
fi
if [[ "$MODE" == --fix ]]; then
  command -v update-desktop-database >/dev/null && update-desktop-database "$USER_APPS" >/dev/null 2>&1 || true
  echo 'Now unpin old RoutePilot favorites, log out/in, then launch RoutePilot from Applications and pin again.'
else
  echo 'Run with --fix to back up and deactivate recognized old per-user launchers.'
fi
