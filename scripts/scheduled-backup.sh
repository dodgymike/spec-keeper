#!/bin/bash
#
# scheduled-backup.sh — wrapper invoked by the launchd LaunchAgent
# (com.specserver.backup). It guarantees `docker` is on PATH (launchd jobs run
# with a minimal environment), runs backup.sh, and appends to a log.
#
# It exits 0 even if the backup is skipped (e.g. Docker not running), so a
# transient failure does not disable the schedule — the next run just retries.

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/backups/backup.log"
mkdir -p "$ROOT/backups"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "[$(ts)] scheduled backup starting" >> "$LOG"
if "$ROOT/scripts/backup.sh" >> "$LOG" 2>&1; then
  echo "[$(ts)] scheduled backup OK" >> "$LOG"
else
  echo "[$(ts)] scheduled backup FAILED (rc=$?) — will retry next run" >> "$LOG"
fi
exit 0
