#!/usr/bin/env bash
#
# dogfood.sh — migrate this repo's own backlog onto the running Spec Server.
#
# This script dogfoods the Spec Server onto itself: it creates the `spec-server`
# project, registers the agent roster, and imports this repo's SPEC.md into the
# server. After it runs, the server is authoritative for the backlog: the
# agents/spec-keeper workflow talks to the API (project slug `spec-server`) as the
# source of truth instead of hand-editing SPEC.md.
#
# It is idempotent and safe to re-run: project creation tolerates 409, agent
# registration is an upsert, and SPEC.md import upserts by task key.
#
# Usage:  scripts/dogfood.sh   (run from anywhere; SPEC.md is resolved from repo root)
# Env:    BASE  — API base URL (default http://localhost:8080/api/v1)

set -euo pipefail

BASE="${BASE:-http://localhost:8080/api/v1}"
JSON="-H Content-Type:application/json"

# Resolve the repo root as the parent of this script's directory, so SPEC.md is
# found regardless of the working directory the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPEC_FILE="$REPO_ROOT/SPEC.md"

# 1. Wait for the server to be ready.
echo "Waiting for the Spec Server to be ready..."
ready=0
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8080/readyz >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "ERROR: server at http://localhost:8080/readyz did not become ready in time." >&2
  exit 1
fi
echo "Server is ready."

# 2. Create the project (tolerate 409 if it already exists).
echo "Creating project 'spec-server'..."
code="$(curl -s -o /dev/null -w '%{http_code}' $JSON \
  -X POST "$BASE/projects" \
  -d '{"slug":"spec-server","name":"Spec Server"}')"
case "$code" in
  2*) echo "  project created." ;;
  409) echo "  project already exists (ok)." ;;
  *) echo "ERROR: creating project returned HTTP $code." >&2; exit 1 ;;
esac

# 3. Register the agent roster (idempotent upsert; tolerate existing).
echo "Registering agents..."
for agent in spec-keeper implementer reviewer security; do
  code="$(curl -s -o /dev/null -w '%{http_code}' $JSON \
    -X POST "$BASE/agents" \
    -d "{\"slug\":\"$agent\",\"display_name\":\"$agent\"}")"
  case "$code" in
    2*) echo "  $agent registered." ;;
    409) echo "  $agent already registered (ok)." ;;
    *) echo "ERROR: registering agent '$agent' returned HTTP $code." >&2; exit 1 ;;
  esac
done

# 4. Import this repo's SPEC.md (idempotent — upserts by task key).
if [ ! -f "$SPEC_FILE" ]; then
  echo "ERROR: SPEC.md not found at $SPEC_FILE." >&2
  exit 1
fi
echo "Importing $SPEC_FILE..."
import_resp="$(curl -s -X POST "$BASE/projects/spec-server/import" \
  --data-binary "@$SPEC_FILE" -H 'Content-Type: text/markdown')"
echo "  $import_resp"

# 5. Print a summary: total task count and todo count.
count_tasks() {
  # Reads a JSON tasks response on stdin and prints the number of tasks.
  python3 -c '
import json, sys
data = json.load(sys.stdin)
if isinstance(data, dict):
    data = data.get("tasks", data.get("items", []))
print(len(data))
'
}

total="$(curl -s "$BASE/projects/spec-server/tasks" | count_tasks)"
todo="$(curl -s "$BASE/projects/spec-server/tasks?status=todo" | count_tasks)"

echo
echo "Summary for project 'spec-server':"
echo "  total tasks: $total"
echo "  todo tasks:  $todo"
echo
echo "The server is now authoritative. spec-keeper should use the API"
echo "(project slug 'spec-server') as the source of truth."
