# Agent API Recipe Book

How an AI agent uses the Spec Server to manage its work. The authoritative, machine-readable
contract is `GET /openapi.json` (Swagger UI at `/docs`); this file is the human-readable map from the
SPEC-driven workflow to concrete calls.

Base URL: `http://localhost:8080/api/v1`. Every request needs `Authorization: Bearer <token>`
under whichever auth mode is configured — see "Authentication" below for which kind of token
and which group permission a given call needs. **Three routes are the exception and are always
public, in every auth mode:** `POST /signup` and `GET /validate` (the human signup queue, HA-7 —
see "Public signup queue" below), plus `POST /agent-enrollments/redeem` (the agent
self-enrollment redeem, ONBOARD-3 — see "Agent self-enrollment" below) — a not-yet-a-user/agent
has no token to present, so each protects itself instead: the human routes with an origin-guard,
a honeypot, a per-IP rate-limit, and optional Turnstile; the agent redeem route with a per-IP
rate-limit and an origin-guard (reusing the same HA-7 guards) plus its own single-use-token burn.

**Storage backend is transparent.** `STORAGE_BACKEND` selects Postgres (default) or DynamoDB; the
HTTP API — every route, status code, and concurrency guarantee (atomic claim, collision-proof
reservation, `If-Match`/412 optimistic locking, lease semantics, idempotency) — is **identical on
both**. Parity is enforced by `tests/test_parity.py`, which runs the same behaviour suite against
both backends. Agents never need to know which backend is live.

## Authentication

Auth is evaluated per request with a precedence ladder (`app/helpers.require_api_key`):

1. **`COGNITO_ISSUER` configured** — every request must carry a valid Cognito RS256 JWT bearer
   token, and the caller's Cognito **group membership** must grant the permission required for
   that request (see the table below). The token's group-list claim (default `cognito:groups`,
   configurable via `AUTH_GROUPS_CLAIM`) is a JSON list; groups map to permissions like this:

   | Group | Permissions |
   |---|---|
   | `spec-admins` | read, write, admin |
   | `spec-writers` | read, write |
   | `spec-readers` | read |

   A caller's effective permissions are the **union** over all its groups. A token with no
   recognized group has no permissions (403 on anything needing read or above).

   Agents authenticate as Cognito **users** (the old M2M client_credentials clients were retired),
   via `USER_PASSWORD_AUTH` — see "Authenticating to the deployed server" below for the recipe.
2. **else `API_KEYS` configured** — the legacy static bearer token, unchanged: send
   `Authorization: Bearer <key>` where `<key>` is one of the comma-separated `API_KEYS` values.
3. **else** — open (local-only default, no `Authorization` header needed).

Only one mode is active at a time: a configured `COGNITO_ISSUER` takes priority over `API_KEYS`,
which takes priority over no auth.

**Method/resource → required permission** (default group names shown; configurable via
`AUTH_GROUP_READ`/`AUTH_GROUP_WRITE`/`AUTH_GROUP_ADMIN`):

| Request | Required permission |
|---|---|
| `GET` / `HEAD` (any resource) | read |
| Mutating calls on `projects` / `agents` | admin |
| All other mutating calls (tasks, epics, reservations, ports, log, chains) | write |
| `GET` / `POST /admin/invites` | admin (both methods — invite listing/minting is admin-only, overriding the default `read` a `GET` would otherwise get) |
| `/admin/users` and `/admin/users/{username}/*` (list/approve/reject/block/unblock/promote/demote/delete) | admin (all methods, overriding the default `read` a `GET` would otherwise get) |
| `/admin/signups` and `/admin/signups/{email_hash}/*` (list/approve/reject) | admin (all methods) |
| `GET`/`POST /admin/agent-enrollments`, `DELETE /admin/agent-enrollments/{token_hash}` | admin (all methods — mint/list/revoke of agent-enrollment tokens is admin-only) |
| `POST /signup`, `GET /validate`, `POST /agent-enrollments/redeem` | **none — always public**, in every auth mode (see above) |

A request succeeds if ANY of the caller's groups grant the required permission — e.g. a
`spec-writers` member has read+write but not admin; a `spec-admins` member has all three.

**Failure modes:** missing/malformed/expired/wrong-audience/wrong-issuer/wrong-`token_use` JWT (or
a missing/wrong static key) → **401**; a valid, verified token whose groups don't grant the
required permission → **403**. Both use the standard flask-smorest `{code, status, message}` error
envelope.

### Authenticating to the deployed server

Locally the server runs with **auth off** — no `Authorization` header, no token, nothing to do.
Everything below matters only against a deployed server that has `COGNITO_ISSUER` set.

**Group → route mapping** (what each call needs; see the table above for the authoritative form):
`GET`/`HEAD` → read (any of `spec-readers`/`spec-writers`/`spec-admins`); task/epic/reservation/
note/commit/log/chain mutations → write (`spec-writers` or `spec-admins`); project/agent admin
(create/update a project or the agent roster) → admin (`spec-admins` only). An agent's Cognito
user is placed in whichever group(s) match the work it does.

**Mint a token by hand** (the raw flow — `USER_PASSWORD_AUTH` against the `agents` app client,
no client secret involved):

```bash
aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id "$AGENTS_CLIENT_ID" \
  --auth-parameters USERNAME="$AGENT_USERNAME",PASSWORD="$AGENT_PASSWORD"
# -> {"AuthenticationResult": {"AccessToken": "...", "RefreshToken": "...", "ExpiresIn": 3600, ...}}
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" "$B/projects/spec-server/tasks?status=todo"
```

**Use the helper** (recommended): `scripts/agent_token.py` runs the `USER_PASSWORD_AUTH` flow
against the `agents` app client (no client secret), **caches the access token in memory, and
renews it via `REFRESH_TOKEN_AUTH` shortly before expiry (or by re-authenticating on a 401)** —
so agents never juggle token lifetimes. It resolves the agent's username/password from an AWS
Secrets Manager secret (`AGENT_CREDENTIALS_SECRET_ARN` or `AGENT_CREDENTIALS_SECRET`, selecting
the user via `AGENT_USERNAME` — optional if the secret has exactly one user) or, for dev/CI,
inline env (`AGENT_USERNAME`, `AGENT_PASSWORD`, `COGNITO_CLIENT_ID`, `COGNITO_REGION`; env values
override the corresponding secret fields). It never prints or logs the password or any token.

```python
from scripts.agent_token import authorized_request, get_token

# One-liner: a self-refreshing Bearer token for a manual call.
status, body = authorized_request("GET", f"{B}/projects/spec-server/tasks?status=todo")

# Or grab the raw token to build your own request:
headers = {"Authorization": f"Bearer {get_token()}"}
```

`authorized_request` retries once on a 401 after re-authenticating, which is exactly the
token-expiry case.

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

## Notes (comments on a task)

Attach timestamped free-text notes to a task — investigation findings, context, hand-off detail.
They're append-only and show up on the task (`GET .../tasks/<id>` includes a `notes` array).

```bash
curl -s -X POST $B/projects/corsearch/tasks/RULEPERF-1/notes \
  -H 'Content-Type: application/json' \
  -d '{"body":"DLQ root cause is the Athena CSV race; see request id abc","author":"alice"}'

curl -s $B/projects/corsearch/tasks/RULEPERF-1/notes     # one task's notes, oldest first
```

Adding a note also emits a `note` event into the project's stream.

**Epic-level notes** (a journal about an epic, not just its tasks):

```bash
curl -s -X POST $B/projects/corsearch/epics/RULEPERF/notes \
  -H 'Content-Type: application/json' \
  -d '{"body":"descoped the ablation sub-epic for v1","author":"planner"}'
curl -s $B/projects/corsearch/epics/RULEPERF/notes      # this epic's notes
```

**List notes across the whole project** (newest first), with filters. The feed merges task and
epic notes; each row is tagged with `scope` (`task`/`epic`) and its `task` or `epic` key:

```bash
curl -s "$B/projects/corsearch/notes"                       # all notes (tasks + epics)
curl -s "$B/projects/corsearch/notes?scope=epic"            # epic notes only
curl -s "$B/projects/corsearch/notes?scope=task"            # task notes only
curl -s "$B/projects/corsearch/notes?author=feature-runner" # by author
curl -s "$B/projects/corsearch/notes?task=RULEPERF-1"       # one task
curl -s "$B/projects/corsearch/notes?epic=RULEPERF"         # one epic
curl -s "$B/projects/corsearch/notes?since=2026-06-30T17:00:00&limit=50"
```

Each row carries `scope`, `task`, `epic`, `author`, `body`, and `created_at`.

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
complete, reserve, note, and chain-run calls emit events automatically — you only POST events for
free-form notes.

Auto-emitted `event_type`s include `chain_run` (a run was started; `payload={"run": "<run
public_id>"}`) and `chain_step` (a step was upserted; `payload={"run": "<run public_id>", "step":
"<step_name>", "status": "<status>"}`), alongside the existing `claimed`/`completed`/`reserved`/
`note`/`decision` kinds.

**Backend note:** `EventOut.task_id` is the internal integer id on Postgres, but is always `null`
on the DynamoDB backend — DynamoDB has no integer surrogate key, so the task reference for a
DynamoDB-backed event is carried in the event's `message`/`payload` instead. This is intentional;
the `EventOut` shape itself is identical on both backends.

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

## Register agents (per project)

The agent registry is **scoped to a project** — each project has its own roster, so two projects can
both have a `spec-keeper`. (The migration scripts do this for you; here's the call.)

```bash
curl -s -X POST $B/projects/corsearch/agents \
  -d '{"slug":"spec-keeper","display_name":"Spec Keeper"}' -H 'Content-Type: application/json'
curl -s $B/projects/corsearch/agents        # this project's roster
```

Registration is metadata + idempotent (upsert by slug within the project); task ownership still works
with any slug string, registered or not.

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

List chain runs, newest first (each with its steps embedded), paginated with `?limit` (default
200, max 1000) and `?offset`:

```bash
curl -s "$B/projects/corsearch/tasks/RULEPERF-1/chain-runs"   # one task's runs
curl -s "$B/projects/corsearch/chain-runs?limit=50"           # every run in the project
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

## Agent self-enrollment (ONBOARD-2/3)

The self-service path for bootstrapping a brand-new **agent** Cognito credential — the agent
counterpart to the human invite/signup flows below. An operator mints a single-use token
(ONBOARD-2, admin-gated); the agent posts it back once (ONBOARD-3, PUBLIC) and gets working
credentials in the same response.

**Mint an enrollment token** — admin-gated (project-admin on `project_slug`, or a global
`spec-admins` member); returns the plaintext token **once**, only its SHA-256 hash is stored:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -X POST $B/admin/agent-enrollments \
  -d '{"project_slug":"corsearch","agent_name":"alice","role":"writer","ttl_seconds":3600}'
# -> {"enrollment_url":"https://spec.elasticninja.com/enroll#token=kX9f...",
#     "token":"kX9f...", "project_slug":"corsearch", "role":"writer",
#     "agent_name":"alice", "expires_at":1753603600}
```
- `role` is one of `reader`/`writer`/`admin` — the project role granted on redemption.
- `ttl_seconds` is optional (60-604800, default `ENROLL_TTL_SECONDS`).
- Returns **501** when `AGENT_ENROLLMENTS_TABLE` is unset (local-dev graceful default).

**List / revoke** (same admin gate; metadata only — never the token or its hash's plaintext):

```bash
curl -s -H 'Authorization: Bearer <admin-token>' "$B/admin/agent-enrollments?project_slug=corsearch"
# -> [{"token_hash":"3f9a...", "project_slug":"corsearch", "agent_name":"alice",
#      "role":"writer", "created_by":"root", "created_at":1753600000,
#      "expires_at":1753603600, "status":"active"}]
curl -s -H 'Authorization: Bearer <admin-token>' -X DELETE $B/admin/agent-enrollments/<token_hash>
# -> 204 (idempotent — revoking an already-used/revoked/unknown token is still a 204)
```
- `token_hash` is the SHA-256 hash of the token — the revocation id / `DELETE` key above. It is
  NOT the plaintext token and cannot be redeemed; the plaintext token is shown only once, by mint.

**Redeem the token** — `POST /api/v1/agent-enrollments/redeem`, **PUBLIC, no auth** (a brand-new
agent holds only the token, nothing else). Atomically burns it (single-use — a missing, already-
used, expired, or raced redeem of the same token all fail identically), then provisions the
agent's Cognito user (`spec-writers` group + membership at the enrolled role on the enrolled
project), and returns working credentials **exactly once**:

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/agent-enrollments/redeem -d '{"token":"kX9f..."}'
# -> 201 {"username":"alice@agents.spec-server.internal", "password":"Ag1!...",
#         "api_base":"https://api.spec.elasticninja.com", "region":"eu-west-1",
#         "client_id":"1agentsclient23id", "project_slug":"corsearch", "role":"writer",
#         "recipe": {"1_mint_token": "...", "2_first_call": "...", "3_migrate_local_backlog": "..."}}
```
- `password` and the full `recipe` — a copy-paste setup guide: mint an access token (via
  `scripts/agent_token.py` or a raw `InitiateAuth` curl), make the first authenticated call (note
  Cloudflare 1010-blocks the default `python-urllib` User-Agent, so send a real one and the
  `Authorization: Bearer` header), then migrate any local backlog into the enrolled cloud project
  — are shown **once** and never stored or logged.
- Failure modes: **400** (generic — missing/used/expired/raced token; no enumeration oracle),
  **429** (rate-limited), **501** (`AGENT_ENROLLMENTS_TABLE` or `COGNITO_USER_POOL_ID` unset),
  **503** (a transient backend fault *before* the burn — the token is still unspent, safe to
  retry), **500** (the token was already spent but provisioning failed afterward — the remedy is
  to mint a fresh enrollment token; tokens are cheap, never un-burn).

Configuration: `AGENT_ENROLLMENTS_TABLE` / `ENROLL_TTL_SECONDS` / `ENROLL_BASE_URL` (mint side,
ONBOARD-2); `ENROLL_API_BASE` / `ENROLL_COGNITO_CLIENT_ID` / `ENROLL_AGENT_DOMAIN` (redeem side,
ONBOARD-3 — describe the deployed API/pool so the returned recipe is copy-paste ready). See
`.env.example`. Infra: `POST /api/v1/agent-enrollments/redeem` is listed in
`local.public_routes` (`infra/terraform/apigw.tf`) so it bypasses the JWT authorizer.

## Admin: invite-only human signup

Two endpoints under `/api/v1/admin`, both gated on the `admin` permission regardless of HTTP
method (see the permission table above) — this is for a human admin/dashboard, not the agent
workflow. Both return **501** when `INVITES_TABLE` is unset (the local-dev default), so a server
without the invites table configured fails gracefully instead of crashing.

**Mint an invite** — returns the plaintext code **once**; only its SHA-256 hash is ever stored:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -X POST $B/admin/invites -d '{"email":"newhire@example.com","ttl_days":14,"approved":false}'
# -> {"code":"kX9f...", "join_url":"https://spec.example.com/join?code=kX9f...",
#     "code_hash":"3fa8...", "expires_at":1753600000, "email_bound":true, "approved":false}
```
- All body fields are optional. Omit `email` for an open (not address-pinned) invite — anyone with
  the code can redeem it; supplying `email` pins the invite to that address (only its hash is
  stored, never the address itself). `ttl_days` (1-90, default `INVITE_TTL_DAYS`) overrides the
  validity window. `approved` just tags the row for a future auto-group-grant hook; it does not by
  itself grant any group today (see below).
- `code` is the **only** response that ever carries the plaintext — it is never stored or logged;
  only `code_hash` (its SHA-256) persists.

**List active invites** — hashes/status/expiry only, never the plaintext:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' $B/admin/invites
# -> [{"code_hash":"3fa8...", "status":"active", "created_at":1752300000,
#      "expires_at":1753600000, "email_bound":true, "approved":false}, ...]
```

**What happens after signup:** the Cognito PreSignUp Lambda (`infra/terraform/invites.tf` +
`infra/terraform/presignup_lambda/handler.py`) hashes the code the invitee submits and atomically
burns the matching row (one conditional update, `active`→`used`; enforces the email binding when
one was set), then auto-confirms and auto-verifies the new user — but adds them to **no group**.
A human is approved by group membership, not a status field, so an admin must still add the new
user to `spec-readers` (or higher) before they can call the API.

## Admin: user lifecycle (HA-5)

Seven endpoints under `/api/v1/admin`, all admin-gated (`spec-admins` group, same as the invites
endpoints above) — again for a human admin/dashboard, not the agent workflow. All return **501**
when `COGNITO_USER_POOL_ID` is unset (the local-dev default), mirroring the invites
501-when-unconfigured contract. These apply equally to **agent** users — an agent is a Cognito
user like any other.

Human "approval" here is by **group membership**, not a status field: a user with no `spec-*`
group is `pending`; a user in at least one is `active`. `enabled` reflects the Cognito
enabled/disabled bit (`false` once blocked/rejected).

**List pool users** (bounded walk, never an unbounded scan — at most 500 users):

```bash
curl -s -H 'Authorization: Bearer <admin-token>' "$B/admin/users?status=pending"
# -> [{"username":"jdoe", "email":"jdoe@example.com", "enabled":true, "status":"pending",
#      "groups":[], "created_at":"2026-07-01T12:00:00+00:00"}, ...]
```
`?status=pending|active` filters by the derived status above; omit it to list everyone.

**Approve a pending user** (adds a group; default `spec-readers`):

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -X POST $B/admin/users/jdoe/approve -d '{"group":"spec-writers"}'
# -> 204
```
`group` is optional (`"spec-readers"` or `"spec-writers"`; default `spec-readers`). Admin
promotion is a separate call (below), not something `approve` can grant.

**Reject / block** — disable the Cognito account AND strip its `spec-*` groups:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -X POST $B/admin/users/jdoe/reject   # -> 204
curl -s -H 'Authorization: Bearer <admin-token>' -X POST $B/admin/users/jdoe/block    # -> 204
```

**Unblock** — re-enable the account. Groups are **not** restored; re-grant with `/approve` or
`/promote`:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -X POST $B/admin/users/jdoe/unblock  # -> 204
```

**Promote / demote** — add/remove `spec-admins`:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -X POST $B/admin/users/jdoe/promote  # -> 204
curl -s -H 'Authorization: Bearer <admin-token>' -X POST $B/admin/users/jdoe/demote   # -> 204
```

**Delete** — hard-delete the user (`AdminDeleteUser`):

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -X DELETE $B/admin/users/jdoe        # -> 204
```

**Guardrails:**
- **404** when `username` doesn't exist in the pool; **409** on an illegal transition.
- **Self-lockout protection:** `block`/`reject`/`delete`/`demote` refuse to act on the *calling*
  admin (409) — you can't block or delete yourself, and `demote` additionally refuses to remove
  the **last** remaining `spec-admins` member (409), so the pool can never end up admin-less.
- Self-protection depends on knowing who the caller is from their verified JWT. Under **static
  `API_KEYS` auth** (no `COGNITO_ISSUER` configured) the caller can't be identified that way, so
  these four self-protected mutations (`block`/`reject`/`delete`/`demote`) return **501** rather
  than run the guard blind — approve/unblock/promote/list are unaffected since they carry no
  self-lockout risk.
- Configured via `COGNITO_USER_POOL_ID` (env); `AWS_REGION` is reused from the existing AWS knobs.

## Public signup queue (HA-7)

The public request→approve human signup path (bird "Path A"): a human requests access, confirms
their email via a single-use magic link, and an admin approves before they're provisioned. This
is for a human landing page, not the agent workflow — but the two intake routes are unauthenticated
public HTTP, so document them precisely to avoid accidental misuse.

**`POST /api/v1/signup`** — PUBLIC, no auth. The uniform-202 anti-enumeration intake:

```bash
curl -s -H 'Content-Type: application/json' \
  -X POST $B/signup -d '{"email":"newhire@example.com","display_name":"New Hire"}'
# -> 202 {"message":"If that email can sign up, we've emailed you a confirmation link.
#          Check your inbox."}
```
- Body: `email` (required), `display_name` (optional, <=64 chars), `turnstile_token` (optional,
  Cloudflare Turnstile response, verified server-side only when `TURNSTILE_SECRET` is
  configured), `hp_website` (honeypot — must stay empty; a non-empty value is silently dropped).
- **Always** returns the identical `202` body for any processable OR silently-dropped request —
  by design there is no way to distinguish unknown / pending / already-registered by status,
  body, or timing (no enumeration oracle). The only other possible outcomes are `400` (grossly
  malformed email) and `429` (per-IP rate-limited — see below); neither is keyed on the email.
- Does **zero** existence work synchronously. Order of guards: origin-guard (opt-in via
  `SIGNUP_ENFORCE_ORIGIN`/`SIGNUP_ALLOWED_ORIGINS`) → honeypot → per-IP DynamoDB fixed-window
  rate-limit (`SIGNUP_RATELIMIT_TABLE`/`MAX`/`WINDOW_S`, fails open) → optional Turnstile
  (`TURNSTILE_SECRET`) → enqueue to SQS (`SIGNUP_INTAKE_QUEUE_URL`). All state-dependent work
  (Cognito existence check, writing the `requested` row, emailing the magic link) happens in the
  async signup worker Lambda draining that queue, off the observable HTTP path.
- Unconfigured (no `SIGNUP_INTAKE_QUEUE_URL`) ⇒ still returns the uniform 202, just without
  enqueuing — a local run degrades gracefully rather than erroring.

**`GET /api/v1/validate?token=<token_id.secret>`** — PUBLIC, no auth. Redeems the magic link the
worker emailed:

```bash
curl -s "$B/validate?token=Yt3f...ab12.9Fq2...Zx0"
# -> 200 {"outcome":"confirmed"}    # or {"outcome":"invalid"}
```
- Every failure mode — missing, malformed, wrong, expired, or already-used token — folds into the
  SAME neutral `"invalid"` outcome (no oracle). A valid re-click of an already-redeemed token is
  idempotently `"confirmed"` (same success page, no second write).
- Constant-time hash compare (`hmac.compare_digest`) + a single conditional single-use flip
  transition the signup row `requested` → `email-validated`.
- Has its own per-IP rate-limit budget, independent of the intake's (a magic-link click never
  eats a submission's allowance).
- Unconfigured (no `SIGNUPS_TABLE`) ⇒ returns the neutral `{"outcome":"invalid"}` rather than
  erroring.

### Admin: the signups bridge

Three endpoints under `/api/v1/admin`, all admin-gated (`spec-admins`, same as invites/users
above). All return **501** when `SIGNUPS_TABLE` is unset, mirroring the invites/users
501-when-unconfigured contract.

**List signup requests** (any state, or filtered; newest first):

```bash
curl -s -H 'Authorization: Bearer <admin-token>' "$B/admin/signups?status=email-validated&limit=50"
# -> [{"email_hash":"3fa8...", "email":"newhire@example.com", "display_name":"New Hire",
#      "status":"email-validated", "created_at":1753600000, "updated_at":1753600300,
#      "validated_at":1753600300, "approved_at":null, "approved_by":null,
#      "rejected_by":null, "reject_reason":null, "provisioned_at":null,
#      "resend_count":0}, ...]
```
`?status=` is one of `requested`, `email-validated`, `admin-approved`, `provisioned`, `rejected`,
`expired`; omit it to list every state. `?limit` bounds rows returned **per state queried**
(default 200, max 1000). Admins see the plaintext `email` (an SSE-KMS-protected attribute value)
to make the call; keys and logs stay hashed (`email_hash`) throughout.

**Approve** — valid ONLY from `email-validated` (409 otherwise: a partial `requested` row must be
validated first); provisions synchronously in the same request:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' \
  -X POST $B/admin/signups/3fa8.../approve
# -> 200 {"email_hash":"3fa8...", "status":"provisioned", "approved_by":"admin-alice",
#         "approved_at":1753600400, "provisioned_at":1753600401, ...}
```
Approve moves `email-validated` → `admin-approved`, mints an approved + email-bound HA-2 invite
(see "Admin: invite-only human signup" above), SES-emails the join link
(`https://spec.elasticninja.com/join?code=...`) to the requester, then stamps the row
`provisioned`. Idempotent: re-approving an already-approved/provisioned row is a no-op that
returns the current row (and retries provisioning if a prior attempt minted-but-didn't-stamp).

**Reject** — valid from any non-terminal state, including a partial `requested` row:

```bash
curl -s -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -X POST $B/admin/signups/3fa8.../reject -d '{"reason":"spam"}'
# -> 200 {"email_hash":"3fa8...", "status":"rejected", "rejected_by":"admin-alice",
#         "reject_reason":"spam", ...}
```
`reason` is optional free text (truncated to 200 chars). Idempotent: re-rejecting an already-
rejected row is a no-op that returns the current row.

**Configuration knobs** (all unset by default so a local run degrades gracefully — see
`.env.example` for the full set): `SIGNUPS_TABLE`, `SIGNUP_INTAKE_QUEUE_URL`,
`SIGNUP_RATELIMIT_TABLE`/`MAX`/`WINDOW_S`, `TURNSTILE_SECRET`, `SIGNUP_PEPPER` (optional HMAC
pepper for `email_hash`; falls back to a plain SHA-256), `SIGNUP_VALIDATE_BASE_URL`,
`SIGNUP_ENFORCE_ORIGIN`/`SIGNUP_ALLOWED_ORIGINS`, `SES_FROM_ADDRESS`/`SES_CONFIG_SET` (reused
from the HA-6 SES setup). Infra: `infra/terraform/signups.tf` + `signup_worker_lambda/` — the
signups DynamoDB table, the rate-limit counter table, the SQS intake queue + DLQ, and the async
signup worker Lambda. **Deferred, not shipped:** an S3 WORM audit bucket and peppered ip/ua
fingerprints (documented as a follow-up, not built).

## Conventions agents must honour

- Claim before you work; complete (or release) when done — never leave a task `in_progress` with no
  lease activity.
- Reserve shared numbers; never choose them.
- Record a `proof_cmd` and a `commit_sha` on completion (definition-of-done).
- Keep your in-flight work under your own `owner`.
