#!/usr/bin/env bash
#
# install-backup-schedule.sh — install/reload the daily-backup LaunchAgent.
#
# Idempotent: copies the plist into ~/Library/LaunchAgents and (re)loads it.
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.specserver.backup.plist
#             rm ~/Library/LaunchAgents/com.specserver.backup.plist

set -euo pipefail

LABEL="com.specserver.backup"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DEST"

# Reload idempotently (ignore "not loaded" on first install).
launchctl unload "$DEST" 2>/dev/null || true
launchctl load -w "$DEST"

echo "Installed and loaded: $LABEL"
echo "  schedule : daily at 03:00 local time"
echo "  plist    : $DEST"
echo "  log      : $(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backups/backup.log"
echo "Test it now : launchctl start $LABEL"
echo "Check state : launchctl list | grep $LABEL"
