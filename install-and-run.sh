#!/usr/bin/env bash
#
# install-and-run.sh — bring up the Spec Server stack (Postgres + Flask app) and
# restore the bird-song backup into it. Idempotent; safe to re-run.
#
#   Run once with root:   sudo bash install-and-run.sh
#
set -euo pipefail

REPO_DIR="/home/mike/source/spec-keeper"
REAL_USER="${SUDO_USER:-mike}"
BACKUP_DIR="/home/mike/spec-server-backups-bird-song/20260711T062210Z"

cd "$REPO_DIR"

# --- pick a compose command -------------------------------------------------
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: no docker compose plugin and no docker-compose binary found." >&2
  exit 1
fi
echo "==> using: $COMPOSE"

# --- .env -------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> created .env from .env.example (spec/spec/specserver)"
fi

# --- build + start ----------------------------------------------------------
echo "==> $COMPOSE up -d --build"
$COMPOSE up -d --build

echo "==> waiting for readiness on http://localhost:8080/readyz ..."
for i in $(seq 1 90); do
  if curl -fsS localhost:8080/readyz >/dev/null 2>&1; then
    echo "    READY: $(curl -s localhost:8080/readyz)"
    break
  fi
  sleep 2
  if [ "$i" = 90 ]; then
    echo "    NOT ready after 180s. Recent logs:"; $COMPOSE logs --tail=40 app
    exit 1
  fi
done

# --- restore the backup -----------------------------------------------------
echo "==> copying backup + restore script into the app container"
$COMPOSE cp "$BACKUP_DIR" app:/tmp/backup
$COMPOSE cp "$REPO_DIR/restore_backup.py" app:/tmp/restore_backup.py

echo "==> running restore inside the app container"
$COMPOSE exec -T app python /tmp/restore_backup.py /tmp/backup "$@"

# --- verify -----------------------------------------------------------------
echo
echo "==> verification (via HTTP API):"
echo -n "    project:  "; curl -s localhost:8080/api/v1/projects | head -c 300; echo
echo -n "    tasks:    "; curl -s "localhost:8080/api/v1/projects/bird-song/tasks?limit=1" | head -c 120; echo
echo -n "    notes:    "; curl -s "localhost:8080/api/v1/projects/bird-song/notes?scope=all&limit=1" | head -c 120; echo

# --- give the repo files back to the real user ------------------------------
chown "$REAL_USER":"$REAL_USER" "$REPO_DIR/.env" 2>/dev/null || true

echo
echo "==> DONE. Spec Server on http://localhost:8080  (Swagger: /docs)"
