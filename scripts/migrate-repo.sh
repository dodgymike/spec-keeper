#!/usr/bin/env bash
#
# migrate-repo.sh — safely migrate ANY repo's SPEC.md onto the running Spec Server.
#
# This is the generic version of dogfood.sh: point it at a project slug and a
# SPEC.md and it will (idempotently, non-destructively) create the project,
# register the standard agent roster, import the SPEC.md, and VERIFY that the
# server's view matches the file. Your SPEC.md is never modified or deleted.
#
# Safety properties:
#   * Idempotent — safe to run repeatedly (project create tolerates 409, agent
#     registration upserts, task import upserts by key).
#   * Non-destructive — never writes or deletes your SPEC.md. The server holds a
#     COPY; the file stays the source until you choose to cut over.
#   * Verifiable — prints an export/diff so you can confirm the server matches
#     the file before trusting it.
#
# Usage:
#   scripts/migrate-repo.sh <slug> [path/to/SPEC.md] [project name]
#
# Examples:
#   scripts/migrate-repo.sh corsearch ../corsearch/SPEC.md "Corsearch"
#   BASE=http://localhost:8080/api/v1 scripts/migrate-repo.sh rss-collector ./SPEC.md
#
# Env:
#   BASE    API base URL          (default http://localhost:8080/api/v1)
#   AGENTS  space-separated roster (default "planner spec-keeper implementer \
#           test-engineer reviewer security documentation")
#   APIKEY  bearer token          (optional; only if the server has API_KEYS set)

set -euo pipefail

SLUG="${1:-}"
SPEC_FILE="${2:-SPEC.md}"
NAME="${3:-$SLUG}"

if [ -z "$SLUG" ]; then
  echo "usage: $0 <slug> [path/to/SPEC.md] [project name]" >&2
  exit 2
fi

BASE="${BASE:-http://localhost:8080/api/v1}"
ROOT="${BASE%/api/v1}"
AGENTS="${AGENTS:-planner spec-keeper implementer test-engineer reviewer security documentation}"

# Portable (bash 3.2-safe): pass the optional bearer token without empty arrays.
# req/code_of inject Authorization only when APIKEY is set.
req() {
  if [ -n "${APIKEY:-}" ]; then
    curl -s -H "Authorization: Bearer $APIKEY" "$@"
  else
    curl -s "$@"
  fi
}
code_of() {
  if [ -n "${APIKEY:-}" ]; then
    curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $APIKEY" "$@"
  else
    curl -s -o /dev/null -w '%{http_code}' "$@"
  fi
}
JSON_HDR='Content-Type: application/json'

# 1. Wait for the server.
echo "Waiting for the Spec Server at $ROOT ..."
for _ in $(seq 1 60); do
  if req -f "$ROOT/readyz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
if [ "${ready:-0}" -ne 1 ]; then
  echo "ERROR: server not ready at $ROOT/readyz. Start it: (cd spec-server && docker compose up -d)" >&2
  exit 1
fi
echo "Server is ready."

# 2. Create the project (idempotent).
echo "Ensuring project '$SLUG' ..."
c="$(code_of -H "$JSON_HDR" -X POST "$BASE/projects" \
  -d "{\"slug\":\"$SLUG\",\"name\":\"$NAME\"}")"
case "$c" in
  2*) echo "  created." ;;
  409) echo "  already exists (ok)." ;;
  *) echo "ERROR: project create returned HTTP $c." >&2; exit 1 ;;
esac

# 3. Register the agent roster (idempotent upsert).
echo "Registering agents: $AGENTS"
for a in $AGENTS; do
  c="$(code_of -H "$JSON_HDR" -X POST "$BASE/agents" \
    -d "{\"slug\":\"$a\",\"display_name\":\"$a\"}")"
  case "$c" in 2*|409) ;; *) echo "ERROR: agent '$a' -> HTTP $c." >&2; exit 1 ;; esac
done
echo "  done."

# 4. Import the SPEC.md (idempotent upsert by task key). Non-destructive.
if [ ! -f "$SPEC_FILE" ]; then
  echo "No SPEC.md at '$SPEC_FILE' — nothing to import (project is ready for new tasks)."
else
  echo "Importing '$SPEC_FILE' (your file is not modified) ..."
  msg="$(req -X POST "$BASE/projects/$SLUG/import" \
    --data-binary "@$SPEC_FILE" -H 'Content-Type: text/markdown')"
  echo "  $msg"

  # 5. VERIFY: diff the file against the server's rendered view.
  echo "Verifying server matches the file (export/diff) ..."
  req -X POST "$BASE/projects/$SLUG/export/diff" \
    --data-binary "@$SPEC_FILE" -H 'Content-Type: text/markdown' \
    | python3 -c 'import sys,json; print("  "+json.load(sys.stdin)["message"])' || true
fi

# 6. Summary.
count() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d if isinstance(d,list) else d.get("tasks",[])))'; }
total="$(req "$BASE/projects/$SLUG/tasks?limit=1000" | count)"
todo="$(req "$BASE/projects/$SLUG/tasks?status=todo&limit=1000" | count)"
echo
echo "Project '$SLUG': $total task(s), $todo todo."
echo "SPEC.md remains a mirror — regenerate it any time with:"
echo "  curl -s $BASE/projects/$SLUG/export > SPEC.md"
echo
echo "Next: agents claim work with claim-next and finish with complete."
echo "See spec-server/INTEGRATION_GUIDE.md for the full workflow."
