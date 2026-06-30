#!/usr/bin/env bash
#
# backup.sh — dump the Spec Server's PostgreSQL database to a compressed file.
#
# The data lives in the Docker named volume `pgdata` (see docker-compose.yml).
# That survives restarts, reboots, and `docker compose down`, but is DESTROYED by
# `docker compose down -v` / `docker volume rm` / `docker volume prune` and is not
# replicated off this machine. Run this script to keep an off-volume snapshot.
#
# Usage:   scripts/backup.sh [output-dir]      (default: spec-server/backups)
# Restore: gunzip -c <file> | docker compose exec -T db psql -U spec -d specserver
#
# Env: POSTGRES_USER (default spec), POSTGRES_DB (default specserver)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT_DIR="${1:-$ROOT/backups}"
DB="${POSTGRES_DB:-specserver}"
DBUSER="${POSTGRES_USER:-spec}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/${DB}-${STAMP}.sql.gz"

mkdir -p "$OUT_DIR"

if ! docker compose ps db >/dev/null 2>&1; then
  echo "ERROR: the 'db' service is not up. Start it: docker compose up -d" >&2
  exit 1
fi

echo "Dumping database '$DB' -> $FILE"
# --clean --if-exists makes the dump safe to restore into an existing database.
docker compose exec -T db pg_dump -U "$DBUSER" -d "$DB" --clean --if-exists \
  | gzip > "$FILE"

# Refuse to keep an empty/failed dump.
if [ ! -s "$FILE" ]; then
  echo "ERROR: backup is empty — dump failed." >&2
  rm -f "$FILE"
  exit 1
fi

# Keep a stable 'latest' pointer for convenience.
ln -sf "$(basename "$FILE")" "$OUT_DIR/${DB}-latest.sql.gz"

echo "Backup OK: $FILE ($(du -h "$FILE" | cut -f1))"
echo "Tables captured:"
gunzip -c "$FILE" | grep -c '^CREATE TABLE' | sed 's/^/  CREATE TABLE statements: /'
echo "Restore with:"
echo "  gunzip -c '$FILE' | docker compose exec -T db psql -U $DBUSER -d $DB"
