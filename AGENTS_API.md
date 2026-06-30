# Agent API Recipe Book

How an AI agent uses the Spec Server to manage its work. The authoritative, machine-readable
contract is `GET /openapi.json` (Swagger UI at `/docs`); this file is the human-readable map from the
SPEC-driven workflow to concrete calls.

Base URL: `http://localhost:8080/api/v1`. If `API_KEYS` is configured, send
`Authorization: Bearer <key>` on every request.

## The workflow → API mapping

| Atomic-increment step | API call |
|---|---|
| Read the backlog | `GET /projects/{slug}/tasks?status=todo` |
| **Pick exactly one task** | `POST /projects/{slug}/tasks/claim-next` |
| Restate / inspect a task | `GET /projects/{slug}/tasks/{id}` (returns `ETag`) |
| Record a discovered follow-up | `POST /projects/{slug}/tasks` |
| Reserve a migration/table/queue number | `POST /projects/{slug}/reservations` |
| Attach a commit / test result | `POST /projects/{slug}/tasks/{id}/commits` |
| Block / defer / supersede | `POST /projects/{slug}/tasks/{id}/status` |
| **Flip the checkbox to done** | `POST /projects/{slug}/tasks/{id}/complete` |
| Give a task back unfinished | `POST /projects/{slug}/tasks/{id}/release` |
| "My specs" | `GET /projects/{slug}/tasks?owner=<me>` |

`{id}` is either the human key (`RULEPERF-9c`) or the task's `public_id`.

## Claim exactly one task (collision-proof)

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/tasks/claim-next \
  -d '{"agent":"alice","priority_max":"P1","epic":"RULEPERF"}'
```
- Returns the claimed task (status now `in_progress`, `owner` = you, plus an `ETag`), or **HTTP 204**
  when nothing is claimable.
- Optional body filters: `epic`, `component`, `priority_max` (only tasks at/above this priority),
  `lease_ttl` (seconds).
- Ordering: priority `P0→P3` (then unprioritized), then `position`, then age.
- **Never** list tasks and pick one yourself — two agents would race onto the same task. `claim-next`
  uses `FOR UPDATE SKIP LOCKED`, so N simultaneous callers get N distinct tasks.

## Complete a task (definition-of-done)

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/tasks/RULEPERF-9c/complete \
  -d '{"commit_sha":"6d0c3ab","repo":"zeal-backend","test_summary":"5/5 jest","proof_cmd":"pytest -k sampler"}'
```
Sets `status=done`, stamps `completed_at`, closes the lease, clears `owner`, and records the commit.
Send `If-Match: "v<version>"` to make it conflict-safe (412 if someone else moved it first).

## Reserve a number (collision-proof)

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/reservations \
  -d '{"namespace":"migration","reserved_by":"alice","note":"add rule_previews index"}'
# -> {"namespace":"migration","value":24,"reserved_by":"alice",...}
```
Each call to the same `namespace` returns the next distinct value — **no two agents ever get the
same number**, even under concurrency. Use the returned `value` to name your resource (e.g.
`migration 024`). Namespaces are independent (`migration`, `table`, `queue`, …).
Inspect: `GET /projects/{slug}/reservations?namespace=migration` · `GET /projects/{slug}/counters`.

## Create a task

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/tasks -d '{
    "key":"RULEPERF-10",          // optional human ID; unique per project
    "title":"cache rule previews",
    "description":"...full body, can include a _Proof:_ line...",
    "epic_key":"RULEPERF",        // optional grouping
    "priority":"P1",              // P0..P3 or omit
    "component":"BE",             // FE/BE/ML/AWS/... free text
    "proof_cmd":"pytest -k preview_cache",
    "tags":["needs-approval"]
  }'
```

## Optimistic locking (avoid lost updates)

1. `GET .../tasks/{id}` → read `version` (also returned as `ETag: "v3"`).
2. `PATCH .../tasks/{id}` with header `If-Match: "v3"`.
3. If another agent changed it meanwhile → **412 Precondition Failed**. Re-read and retry.

`If-Match` is optional (lenient for single-agent use) but recommended whenever you read-then-write.

## Filtering ("my specs" and more)

```
GET /projects/{slug}/tasks?owner=alice          # one agent's specs
GET /projects/{slug}/tasks?status=in_progress
GET /projects/{slug}/tasks?epic=RULEPERF&priority=P0
GET /projects/{slug}/tasks?tag=needs-approval
GET /projects/{slug}/tasks?q=preview            # free-text on title/description
```

## Relations

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/tasks/RULEPERF-10/relations \
  -d '{"target":"RULEPERF-4","kind":"supersedes"}'   # blocks | supersedes | relates | follow_up
```
`supersedes` also sets the target's status to `superseded` and links it back.

## Status keywords

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/projects/corsearch/tasks/EC2-PROD-SCALE/status \
  -d '{"status":"blocked","note":"blocked on RISE AWS access"}'
```
Statuses: `todo`, `in_progress`, `blocked`, `deferred`, `done`, `superseded`, `cancelled`.

## Migrate a SPEC.md in and out (round-trip)

Adopt incrementally: import an existing `SPEC.md`, run file-and-server in parallel, then go
server-only. Bodies are raw `text/markdown`.

```bash
# Import a repo's SPEC.md into the server (idempotent — safe to re-run):
curl -s -X POST $B/projects/corsearch/import \
  --data-binary @SPEC.md -H 'Content-Type: text/markdown'
# -> {"message":"imported: 43 task(s) created, 0 updated; ..."}

# Render the backlog back to a SPEC.md mirror:
curl -s $B/projects/corsearch/export > SPEC.md

# Dry-run: what would change vs a local SPEC.md (adoption safety)?
curl -s -X POST $B/projects/corsearch/export/diff \
  --data-binary @SPEC.md -H 'Content-Type: text/markdown'
# -> {"message":"diff vs posted: 0 new ([]), 0 only-in-server ([]), 1 changed (['API-2'])."}
```

The parser understands the observed dialects: `[ ] [~] [x] [-]` checkboxes, `**KEY · Title**`,
epic headings (`### EPIC NAME — desc`), trailing `(BE, P0, blocked)` metadata, and `_Proof: <cmd>_`
lines. Tasks are keyed by their human ID, so import upserts rather than duplicates.

## Log your work and record decisions

The append-only event stream replaces `AGENT_LOG.md`; decisions replace `DECISIONS.md`. Claim,
complete, and reserve calls emit events automatically — you only POST events for free-form notes.

```bash
# Free-form note:
curl -s -X POST $B/projects/corsearch/events \
  -d '{"event_type":"note","agent":"alice","message":"DLQ drained; root cause was X"}' \
  -H 'Content-Type: application/json'

# Read the stream (newest first; filter by type/agent/task, paginate with limit/offset):
curl -s "$B/projects/corsearch/events?event_type=completed&limit=20"

# Record a decision (also emits a 'decision' event):
curl -s -X POST $B/projects/corsearch/decisions -H 'Content-Type: application/json' -d '{
  "key":"DEC-7","title":"Adopt Aurora","decision":"Use Aurora Serverless v2.",
  "context":"Spiky load.","consequences":"Cold-start latency on scale-to-zero."}'
curl -s $B/projects/corsearch/decisions
```

## Track the mandated chain (spec-keeper → … → security)

Record each pass of a task through the agent chain, with a justification required for any skip.

```bash
# Start a run for a task:
RUN=$(curl -s -X POST $B/projects/corsearch/tasks/RULEPERF-1/chain-runs \
  -d '{"started_by":"feature-runner"}' -H 'Content-Type: application/json' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['public_id'])")

# Record each step (PUT is an upsert by step name):
curl -s -X PUT $B/projects/corsearch/chain-runs/$RUN/steps/implementer \
  -d '{"status":"passed","step_order":2,"agent":"implementer"}' -H 'Content-Type: application/json'

# A skipped step MUST carry a justification, else 422:
curl -s -X PUT $B/projects/corsearch/chain-runs/$RUN/steps/security \
  -d '{"status":"skipped","skip_justification":"docs-only change"}' -H 'Content-Type: application/json'

# Close the run:
curl -s -X PATCH $B/projects/corsearch/chain-runs/$RUN -d '{"status":"passed"}' -H 'Content-Type: application/json'
```

## Make claim/reserve safe to retry (idempotency)

If a network blip makes you unsure whether a `claim-next` or `reservations` POST landed, retry it
with the same `Idempotency-Key` header — the server replays the original result instead of claiming
a second task or burning a second number.

```bash
curl -s -X POST $B/projects/corsearch/tasks/claim-next \
  -H 'Idempotency-Key: claim-2026-06-30-001' -H 'Content-Type: application/json' \
  -d '{"agent":"alice"}'
# Re-sending the identical request with the same key returns the SAME task.
```

## Conventions agents must honour

- Claim before you work; complete (or release) when done — never leave a task `in_progress` with no
  lease activity.
- Reserve shared numbers; never choose them.
- Record a `proof_cmd` and a `commit_sha` on completion (definition-of-done).
- Keep your in-flight work under your own `owner`.
