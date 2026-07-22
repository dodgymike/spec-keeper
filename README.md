# Spec Server

A local, concurrency-safe **task/spec management server for AI coding agents**. It replaces the
fragile flat-`SPEC.md` workflow (manually picking the next unchecked box, hand-reserving migration
numbers, append-only file locking) with a small REST API backed by PostgreSQL.

Built with **Python + Flask (flask-smorest) + SQLAlchemy + PostgreSQL**, runs in **Docker**, and
auto-publishes an **OpenAPI 3** contract that agents consume directly. The storage layer is
pluggable (`STORAGE_BACKEND=postgres|dynamodb`, default `postgres`); the public API is identical
either way — see "Architecture" below.

## Why it exists

Multiple agents working one repo through a `SPEC.md` file hit two recurring races:

1. **Two agents pick the same "next task."** Solved by `POST /tasks/claim-next` —
   `SELECT ... FOR UPDATE SKIP LOCKED` hands each caller a *distinct* task.
2. **Two agents grab the same migration/table number** (the real "LOC-10 and FLEET-9 both grabbed
   024" bug). Solved by `POST /reservations` — an `INSERT ... ON CONFLICT DO UPDATE RETURNING`
   atomic counter, with a `UNIQUE(project, namespace, value)` backstop.

Plus optimistic locking (`version`/`If-Match` → 412) so concurrent edits never silently clobber.

## Each agent keeps its specs separate

A **single shared backlog per project**; every task carries an `owner`. Claiming a task stamps your
agent slug and an exclusive lease. "My specs" is just `GET /tasks?owner=<me>`. Two agents can never
hold the same task. (See `DECISIONS.md` DEC-1.)

## Quick start

```bash
cp .env.example .env
docker compose up -d --build

curl -s localhost:8080/readyz                 # {"status":"ready"}
open http://localhost:8080/docs               # Swagger UI
curl -s localhost:8080/openapi.json | jq .    # the machine-readable contract for agents
```

### A 60-second tour

```bash
B=http://localhost:8080/api/v1
J='-H Content-Type:application/json'

curl -s $J -X POST $B/projects -d '{"slug":"corsearch","name":"Corsearch"}'
curl -s $J -X POST $B/projects/corsearch/epics -d '{"key":"RULEPERF","title":"Rule perf"}'
curl -s $J -X POST $B/projects/corsearch/tasks \
  -d '{"key":"RULEPERF-1","title":"rank poor rules","epic_key":"RULEPERF","priority":"P0"}'

# An agent claims exactly one task (collision-proof) and later completes it:
curl -s $J -X POST $B/projects/corsearch/tasks/claim-next -d '{"agent":"alice"}'
curl -s $J -X POST $B/projects/corsearch/tasks/RULEPERF-1/complete \
  -d '{"commit_sha":"deadbeef","test_summary":"5/5 pass","proof_cmd":"pytest -k ranking"}'

# Reserve a collision-proof migration number:
curl -s $J -X POST $B/projects/corsearch/reservations -d '{"namespace":"migration"}'  # -> {"value":1,...}
```

Full agent recipe book: **`AGENTS_API.md`**.
Migrating an existing repo's `SPEC.md` onto the server? See **`INTEGRATION_GUIDE.md`** and
`scripts/migrate-repo.sh <slug> <path/to/SPEC.md>`.

**Auth is off by default** (local-only, no `Authorization` header needed). Set `COGNITO_ISSUER`
to require a Cognito RS256 JWT whose `cognito:groups` claim grants the permission the request
needs — `spec-readers` (read), `spec-writers` (read, write), `spec-admins` (read, write, admin) —
or set `API_KEYS` for the simpler legacy static-bearer-token mode — Cognito takes precedence if
both are set. **CORS is off by default** too; set `CORS_ORIGINS` (an exact-match allow-list, never
`*`) to let the dashboard call the API from a browser. See `AGENTS_API.md` → "Authentication" for
the full precedence ladder, the group table, and how to mint a token, and `.env.example` for
every knob.

Three routes are **always** public regardless of the auth mode — `POST /api/v1/signup` and
`GET /api/v1/validate` (the public request→approve signup queue, HA-7; see "Public signup queue"
below), plus `POST /api/v1/agent-enrollments/redeem` (the agent self-enrollment redeem,
ONBOARD-3; see "Agent self-enrollment" below) — since a not-yet-a-user/agent has no token to
present. They are deliberately excluded from `require_api_key` and instead protect themselves:
the signup routes with an origin-guard, a honeypot, a per-IP rate-limit, and (optionally)
Cloudflare Turnstile; the redeem route with a per-IP rate-limit and an origin-guard (reusing the
same HA-7 guards) plus an atomic single-use token burn.

## What's included

- **Tasks** — CRUD, atomic `claim-next`, `complete`, `release`, `status`, relations, commit refs;
  per-task `owner`/lease; optimistic locking (`version`/`If-Match` → 412).
- **Atomic number reservation** — collision-proof per-namespace counters.
- **Per-project agent registry** — each project has its own roster (two projects can both have a
  `spec-keeper`).
- **SPEC.md round-trip** — import an existing `SPEC.md`, export the DB back to one (`app/specmd.py` +
  `blueprints/ports.py`); the migration bridge.
- **Pluggable storage backend** — the same REST API runs on Postgres (default) or DynamoDB
  (`STORAGE_BACKEND=dynamodb`), with identical atomic-claim, atomic-reservation, and
  optimistic-lock (`If-Match`/412) guarantees on both (`app/storage/`).
- **Append-only event log + decision records** — replace `AGENT_LOG.md` / `DECISIONS.md`; events are
  auto-emitted on claim/complete/reserve/note/chain-run.
- **Chain-run tracking** — record a task's pass through the mandated agent chain; a skipped step
  needs a justification. List a task's runs or every run in the project (with steps), paginated.
- **Idempotency-Key** replay on `claim-next`/`reserve`; **lease reaper** (abandoned tasks become
  re-claimable); **pagination** on list endpoints.
- **Invite-only human signup admin endpoints** (HA-2) — mint/list single-use invite codes
  (hash-only storage, admin-gated); see "Invite-only human signup" below.
- **Admin user-lifecycle endpoints** (HA-5) — list/approve/reject/block/unblock/promote/demote/
  delete Cognito pool users (including agent users), admin-gated; see "Admin: user lifecycle"
  below.
- **Agent self-enrollment** (ONBOARD-2/3) — an admin mints a single-use enrollment token
  (`POST /api/v1/admin/agent-enrollments`); a brand-new agent redeems it once, PUBLIC/no-auth
  (`POST /api/v1/agent-enrollments/redeem`), which atomically burns the token then provisions a
  real Cognito credential + project membership and returns working creds + a setup recipe. See
  "Agent self-enrollment" below.
- **Public request→approve signup queue** (HA-7) — a uniform-202, anti-enumeration
  `POST /api/v1/signup` intake + `GET /api/v1/validate` magic-link redeem (both PUBLIC, no
  auth), decoupled behind SQS to an async worker Lambda, plus an admin bridge
  (`/api/v1/admin/signups*`) to list/approve/reject requests; approval synchronously mints an
  HA-2 invite and emails the join link. See "Public signup queue" below.
- **Alembic migrations**, **OpenAPI 3** + Swagger UI, **Docker** compose, and a **scheduled daily
  backup** (`scripts/backup.sh` via a launchd LaunchAgent).

## Architecture

| Piece | File |
|---|---|
| App factory + CLI (`flask init-db`) | `app/__init__.py` |
| Env config | `app/config.py` |
| SQLAlchemy models (the schema) | `app/models.py` |
| Marshmallow schemas (validation **and** OpenAPI source of truth) | `app/schemas.py` |
| Atomic claim + reserve, event-log helper | `app/services.py` |
| Storage abstraction (backend-neutral port + Postgres/DynamoDB adapters) | `app/storage/` |
| Idempotency-Key store | `app/idempotency.py` |
| `SPEC.md` import/export parser + renderer | `app/specmd.py` |
| Signup queue primitives (normalize/hash email, mint/verify magic-link token, state machine, conditional DynamoDB writes) | `app/signup.py` |
| Signup queue boto3 glue (signups table, SQS enqueue, SES send) | `app/signup_aws.py` |
| Per-IP fixed-window rate limiter for the public signup routes | `app/signup_ratelimit.py` |
| REST blueprints (projects · agents · epics · tasks · reservations · ports · log · chains · admin · signup · enroll) | `app/blueprints/` |
| Alembic migrations (run on boot) | `migrations/` |
| Tests (concurrency + round-trip + idempotency) | `tests/` |
| Backup / migrate / schedule scripts | `scripts/` |

Blueprints call `current_app.storage.<method>()` instead of touching `db.session` directly.
`app/storage/` holds the abstraction: `base.py` (the `StorageBackend` `Protocol` — the full
method set every adapter must satisfy), `errors.py` (`NotFound`/`Conflict`/`VersionConflict`/
`BackendUnavailable`, mapped to `404`/`409`/`412`/`503`), `dto.py` (frozen DTOs returned in place
of ORM objects), `postgres.py` (the reference adapter — still delegates the two atomic
operations to the unchanged `app/services.py`), and `dynamo.py` (the DynamoDB adapter, over
boto3 and a single table with 5 GSIs; key/GSI encoders live in `keys.py`). `make_storage()` in
`app/storage/__init__.py` picks the adapter from the `STORAGE_BACKEND` config (default
`"postgres"`; `"dynamodb"` selects `DynamoBackend`). The public HTTP API is identical on both
backends — this is an internal refactor, not a contract change. Design:
`STORAGE_ABSTRACTION_DEEPDIVE.md` §3; infra mirror: `infra/terraform/dynamodb.tf`.

The DynamoDB adapter reads its own settings straight from `os.environ` (not `app/config.py`):
`DYNAMODB_TABLE`, `DYNAMODB_ENDPOINT_URL` (point this at DynamoDB Local for dev/test),
`AWS_REGION`, and standard AWS credential env vars. This keeps `STORAGE_BACKEND=dynamodb`
a drop-in choice with zero effect on the default Postgres path.

Key tables: `projects`, `agents` (project-scoped), `epics`, `tasks` (status enum, priority,
component, `owner`, `version`, lease, `section`), `tags`, `task_relations`, `commit_refs`,
`counters` + `reservations` (atomic numbering), `leases` (one active per task), `events`,
`decisions`, `chain_runs` + `chain_steps`, `idempotency_keys`.

## Running the tests

The Postgres-implementation-specific guarantees (skip-locked dequeue, on-conflict upsert, partial
unique indexes) require a real PostgreSQL, so the default suite runs against that:

```bash
docker compose exec db psql -U spec -d specserver -c "CREATE DATABASE specserver_test;"
docker compose exec -T -e TEST_DATABASE_URL=postgresql+psycopg://spec:spec@db:5432/specserver_test \
  app python -m pytest -q
# -> 68 passed
```

On boot the container runs `alembic upgrade head` (adopting a legacy `create_all` database by
stamping it first); the test suite builds its schema directly from the models.

### Cross-backend parity (Postgres + DynamoDB)

`tests/conftest.py` parametrises the `app` fixture over `TEST_BACKENDS` (comma-separated, default
`postgres`). Set it to `postgres,dynamodb` to run the whole suite against both adapters — the
fixture creates the single DynamoDB table + 5 GSIs (mirroring `infra/terraform/dynamodb.tf`) on
DynamoDB Local and tears it down per session. The 3 tests that assert on SQLAlchemy/ORM
internals are marked `postgres_only` and skip on the `dynamodb` param; `tests/test_parity.py`
holds their backend-neutral HTTP-only equivalents (no-collision claim, contiguous reservation,
`If-Match`/412, lease reclaim — proven identical on both backends).

Bring up DynamoDB Local with the separate overlay (`docker-compose.dynamodb.yml` — the main
`docker-compose.yml` is untouched) and run both backends:

```bash
docker compose -f docker-compose.yml -f docker-compose.dynamodb.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.dynamodb.yml exec -T \
  -e TEST_DATABASE_URL=postgresql+psycopg://spec:spec@db:5432/specserver_test \
  -e TEST_BACKENDS=postgres,dynamodb \
  app python -m pytest -q
# -> 110 passed, 3 skipped
```

(`DYNAMODB_ENDPOINT_URL` / `AWS_*` are already set by the overlay.) The 3 skips are the
`postgres_only` trio; everything else — including the two atomic guarantees and the
optimistic-lock/412 contract — passes identically on both backends.

## Configuration (`.env`)

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://spec:spec@db:5432/specserver` | SQLAlchemy connection |
| `STORAGE_BACKEND` | `postgres` | Storage adapter selected by `make_storage()`. `postgres` (default) or `dynamodb` (`app/storage/dynamo.py`). |
| `DYNAMODB_TABLE` | _(none)_ | Table name for the `dynamodb` backend. Read directly from `os.environ` by the storage layer (not `app/config.py`). Required when `STORAGE_BACKEND=dynamodb`. |
| `DYNAMODB_ENDPOINT_URL` | _(none)_ | Override endpoint for the `dynamodb` storage backend and (separately, via `app/config.py`) the invites admin endpoints, e.g. `http://dynamodb-local:8000` for DynamoDB Local. Unset ⇒ boto3 talks to real AWS. |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | _(none)_ | Standard AWS/boto3 credential env vars, used by the `dynamodb` storage backend and the invites admin endpoints. |
| `INVITES_TABLE` | _(none)_ | Dedicated DynamoDB table backing invite-only human signup (HA-2): `POST`/`GET /api/v1/admin/invites`. Unset ⇒ both endpoints return 501 (local-dev graceful default). Wired from terraform output `invites_table_name` (`infra/terraform/invites.tf`). |
| `INVITE_TTL_DAYS` | `14` | Default validity window (days) for a freshly minted invite; overridable per-invite via the mint request's `ttl_days` (1-90). |
| `INVITE_JOIN_BASE_URL` | _(empty)_ | Base URL prefixed to the `join_url` the mint endpoint returns (e.g. `https://spec.example.com`); empty ⇒ a relative `/join?code=...` link. |
| `AGENT_ENROLLMENTS_TABLE` | _(none)_ | Dedicated DynamoDB table backing agent self-enrollment (ONBOARD-2/3): `POST`/`GET`/`DELETE /api/v1/admin/agent-enrollments*` (mint/list/revoke) and the public `POST /api/v1/agent-enrollments/redeem`. Unset ⇒ all of these return 501 (local-dev graceful default). Wired from terraform output `agent_enrollments_table_name`. |
| `ENROLL_TTL_SECONDS` | `3600` | Default validity window (seconds) for a freshly minted enrollment token; overridable per-mint via the mint request's `ttl_seconds` (60-604800). |
| `ENROLL_BASE_URL` | `https://spec.elasticninja.com` | Base URL the mint endpoint's `enrollment_url` is built from (the plaintext token rides in the fragment, `#token=...`). |
| `ENROLL_API_BASE` | `https://api.spec.elasticninja.com` | API base URL the redeem endpoint hands back in its response/recipe, so a newly-enrolled agent knows where to call. |
| `ENROLL_COGNITO_CLIENT_ID` | _(none)_ | Cognito app-client id (`USER_PASSWORD_AUTH`, no client secret) the redeem response/recipe tells the new agent to mint tokens against. Unset ⇒ omitted from the recipe. Wired from terraform. |
| `ENROLL_AGENT_DOMAIN` | `agents.spec-server.internal` | Email-as-username domain for agent sign-in aliases: the redeem endpoint provisions a project-namespaced `<sanitized-agent-name>.<sanitized-project-slug>.<16-hex-digest>@<ENROLL_AGENT_DOMAIN>` (ONBOARD-3a — the same `agent_name` in different projects gets a different Cognito user; the 19 platform agents provisioned by `scripts/enrol_agents.py` still use the plain `<name>@<domain>` scheme and are unaffected). |
| `COGNITO_USER_POOL_ID` | _(none)_ | Cognito user pool backing the admin user-lifecycle endpoints (HA-5): `/api/v1/admin/users*` (list/approve/reject/block/unblock/promote/demote/delete). Unset ⇒ every `/admin/users*` endpoint returns 501 (local-dev graceful default), same contract as `INVITES_TABLE` above. Reuses `AWS_REGION`. Wired from terraform output `cognito_user_pool_id` (`infra/terraform/cognito.tf`). |
| `SIGNUPS_TABLE` | _(none)_ | Dedicated DynamoDB table backing the public signup queue (HA-7): `GET /api/v1/validate` and the admin `/api/v1/admin/signups*` bridge. Unset ⇒ validate returns the neutral `invalid` outcome and the admin endpoints return 501 (same graceful-default contract as `INVITES_TABLE`). Wired from terraform output `signups_table_name` (`infra/terraform/signups.tf`). |
| `SIGNUP_INTAKE_QUEUE_URL` | _(none)_ | SQS queue URL the public `POST /api/v1/signup` intake enqueues to. Unset ⇒ intake still returns its uniform 202 without enqueuing (local-dev graceful default). Wired from terraform output `signup_intake_queue_url`. |
| `SQS_ENDPOINT_URL` | _(none)_ | Endpoint override for the SQS client used by the intake enqueue (e.g. a local SQS emulator). Unset ⇒ boto3 talks to real AWS. |
| `SIGNUP_RATELIMIT_TABLE` | _(none)_ | Per-IP fixed-window DynamoDB counter table for the public signup routes (`${name_prefix}-signup-ratelimit`, terraform output `signup_ratelimit_table_name`). Unset ⇒ the in-app limiter fails open (the CDN/edge limiter is the durable backstop). |
| `SIGNUP_RATELIMIT_MAX` | `5` | Max requests per source IP per window before a 429, for `POST /signup` and (independently) `GET /validate`. |
| `SIGNUP_RATELIMIT_WINDOW_S` | `60` | The fixed per-IP rate-limit window, in seconds. |
| `TURNSTILE_SECRET` | _(none)_ | Cloudflare Turnstile server-side secret. Set ⇒ `POST /signup` verifies the submitted `turnstile_token` server-side (a failed/absent token is silently dropped as a bot, still returning the uniform 202). Unset (dev default) ⇒ the Turnstile check is skipped entirely. |
| `SIGNUP_PEPPER` | _(none)_ | Optional pepper for `email_hash` (`HMAC-SHA256(pepper, email)` instead of a plain `SHA-256`), defeating offline dictionary reversal of a leaked signups table. Must match between the app and the signup worker Lambda. Unset ⇒ a plain SHA-256 hash (fine for local dev). |
| `SIGNUP_VALIDATE_BASE_URL` | _(empty)_ | Base URL the signup worker prefixes to the magic-link validation URL it emails (e.g. `https://spec.elasticninja.com/validate?token=...`); empty ⇒ a relative link. |
| `SIGNUP_ENFORCE_ORIGIN` | `false` | When `true` AND `SIGNUP_ALLOWED_ORIGINS` is non-empty, `POST /signup` requires the `Origin` (or `Referer` host) to match one of the allowed origins, else 403. Off by default (dev). |
| `SIGNUP_ALLOWED_ORIGINS` | _(empty)_ | Comma-separated exact-match origin allow-list used only when `SIGNUP_ENFORCE_ORIGIN=true`, e.g. `https://spec.elasticninja.com`. |
| `SES_FROM_ADDRESS` / `SES_CONFIG_SET` | _(none)_ | Verified SES sender address + configuration set, reused from the HA-6 transactional-email setup, for the signup-approve join-link email. Unset ⇒ the send is skipped (logged), so approve still provisions in dev, just without an email. |
| `LEASE_DEFAULT_TTL` | `1800` | Claimed-task lease seconds |
| `API_KEYS` | _(empty)_ | Comma-separated bearer tokens (legacy static auth). Empty ⇒ auth off (local-only). Ignored if `COGNITO_ISSUER` is set. |
| `COGNITO_ISSUER` | _(empty)_ | OIDC issuer for Cognito RS256 JWT auth (AUTH-2/AUTH-10). When set, takes precedence over `API_KEYS`. Authorization is by Cognito group membership (`AUTH_GROUPS_CLAIM`, `AUTH_GROUP_READ`/`WRITE`/`ADMIN`), not scopes. See `AGENTS_API.md` → "Authentication" and `.env.example` for the full `COGNITO_*`/`JWKS_*`/`AUTH_GROUP_*` knob set. |
| `CORS_ORIGINS` | _(empty)_ | Comma-separated exact-match browser-origin allow-list for the dashboard (AUTH-7). Empty ⇒ CORS off. `*` is never honoured. |
| `AGENT_CREDENTIALS_SECRET_ARN` | _(empty)_ | **Agent-side, not server.** Secrets Manager ARN holding the `agent-credentials` secret (pool id, client id, region, and a map of agent usernames to passwords/groups); read by `scripts/agent_token.py` to authenticate an agent user against a deployed server. Prefer this over the inline `AGENT_*` fields. |

### Secrets & tokens

- Agents authenticate as Cognito **users** (the M2M client_credentials clients were retired to
  save cost). Their usernames/passwords live in the `agent-credentials` AWS Secrets Manager
  secret — JSON shaped `{"pool_id", "client_id", "region", "users": {"<name>": {"password",
  "groups"}}}` — **never** in the repo, in `*.tfvars`, in terraform outputs, or in git.
- **The server needs no secret at rest.** It authenticates callers by validating their JWT against
  Cognito's **public** JWKS (`COGNITO_JWKS_URI`) and checking the token's `cognito:groups` claim;
  it never holds a client secret or a user password. Only agents hold credentials, and only to
  authenticate and mint tokens.
- `.env` is **gitignored**; `.env.example` documents every knob with safe empty defaults. Set
  Cognito/agent values in your local `.env` (or inject at deploy time), never in a committed file.
- Authenticating/refreshing tokens for API calls is `scripts/agent_token.py` — it runs
  `USER_PASSWORD_AUTH` against the `agents` app client, keeps the access token in memory, renews
  it (via `REFRESH_TOKEN_AUTH` or by re-authenticating on a 401), and never prints or logs the
  password or any token. See `AGENTS_API.md` → "Authenticating to the deployed server".

### Invite-only human signup (HA-2)

Admins mint single-use invite codes (`POST /api/v1/admin/invites`, admin-gated) that a human
redeems at signup; only the invite's SHA-256 hash is ever stored server-side and the plaintext
code is returned once, never logged. This needs `INVITES_TABLE` (a dedicated DynamoDB table,
terraform output `invites_table_name` from `infra/terraform/invites.tf`) — unset ⇒ both admin
endpoints return 501. The same terraform file builds the Cognito PreSignUp Lambda that burns the
code at signup (auto-confirm/verify, no group added) and outputs `presignup_lambda_arn`; that ARN
is wired as the user pool's `pre_sign_up` trigger by the separate HA-3 pool cutover, which owns
`cognito.tf` (this file never edits it). See `AGENTS_API.md` → "Admin: invite-only human signup"
for the request/response shapes.

### Admin: user lifecycle (HA-5)

Once a human (or agent) is a Cognito user, an admin manages their access by group membership:
`GET /api/v1/admin/users` lists pool users (bounded to at most 500, never an unbounded scan) with
a derived `pending`/`active` status (`pending` = in no `spec-*` group); `approve` grants
`spec-readers`/`spec-writers`; `promote`/`demote` add/remove `spec-admins`; `reject`/`block`
disable the account and strip its `spec-*` groups; `unblock` re-enables it (groups are not
restored); `DELETE` hard-deletes the user. All seven endpoints are admin-gated and need
`COGNITO_USER_POOL_ID` — unset ⇒ 501, the same graceful-default contract as the invites table
above. Self-lockout guards refuse to let an admin block/reject/delete/demote themselves, and
refuse to demote the last remaining admin; those guarded mutations need the caller's verified
Cognito identity, so they return 501 under static `API_KEYS` auth (no `COGNITO_ISSUER`). See
`AGENTS_API.md` → "Admin: user lifecycle" for the request/response shapes.

### Agent self-enrollment (ONBOARD-2/3)

The agent counterpart to the invite/signup flows above: an admin mints a single-use enrollment
token (`POST /api/v1/admin/agent-enrollments`, admin-gated on the target `project_slug`) and hands
it to a brand-new agent, which redeems it exactly once (`POST /api/v1/agent-enrollments/redeem`,
**PUBLIC, no auth** — a not-yet-a-credential agent has no token to authenticate with).

Mint refuses (**409**) to create a second token while an active, unexpired enrollment already
exists for the same `(project_slug, agent_name)` — a best-effort guard against two concurrent live
tokens for one target; a prior enrollment that is used/expired/revoked never blocks a fresh mint.

Redeem atomically **burns** the token first (a conditional DynamoDB update, `active`→`used`,
under `expires_at > now`; a missing/used/expired/raced token all fail identically — no
enumeration oracle), then **provisions** the agent's Cognito user (`AdminCreateUser` →
`AdminSetUserPassword` permanent → `AdminAddUserToGroup spec-writers`) and grants it membership on
the enrolled project at the enrolled role, and returns the working username/password + a
copy-paste setup recipe in the same response — the password is shown **once** and never stored or
logged. The provisioned username is **project-namespaced** (ONBOARD-3a):
`<sanitized-agent-name>.<sanitized-project-slug>.<16-hex-digest>@<ENROLL_AGENT_DOMAIN>`, so the
same `agent_name` redeemed into different projects always provisions a *different* Cognito user
(cross-tenant isolation), while re-enrolling the same `(project_slug, agent_name)` rotates the
password on the *same* user. If provisioning fails after the burn, the token stays spent (500) and
the remedy is to mint a fresh one; burn-then-provision never un-burns. Both endpoints return
**501** when `AGENT_ENROLLMENTS_TABLE` (mint/list/revoke/redeem) or `COGNITO_USER_POOL_ID`
(redeem's provisioning step) is unset — the same graceful-default contract as the
invites/user-lifecycle endpoints above. The redeem route reuses the HA-7 per-IP rate-limit and
origin-guard. See `AGENTS_API.md` → "Agent self-enrollment" for the request/response shapes, and
`infra/terraform/apigw.tf` (`local.public_routes`) for how the redeem route bypasses the JWT
authorizer.

### Public signup queue (HA-7)

The public self-service path (bird "Path A"): a human requests access, confirms their email via a
magic link, and an admin approves before they're provisioned — decoupled behind SQS so the public
HTTP path never does existence-dependent work (the enumeration-privacy crux).

- `POST /api/v1/signup` — **PUBLIC, no auth.** The uniform-202 intake: always returns the same
  `202 {"message": "If that email can sign up, we've emailed you a confirmation link. Check your
  inbox."}` for any processable or silently-dropped request — no enumeration oracle by body,
  status, or timing. Order of the cheap synchronous guards: origin-guard → honeypot
  (`hp_website`) → per-IP DynamoDB fixed-window rate-limit (fails open; 429 over budget) →
  optional Cloudflare Turnstile (verified server-side only when `TURNSTILE_SECRET` is set) →
  enqueue to SQS. All existence-dependent work (Cognito check, row create, magic-link email)
  happens in the async signup worker Lambda off SQS, which an attacker can neither observe nor
  time.
- `GET /api/v1/validate?token=<token_id.secret>` — **PUBLIC, no auth.** Redeems the single-use
  magic link: `200 {"outcome": "confirmed"}` or `{"outcome": "invalid"}` — every failure mode
  (missing/malformed/wrong/expired/already-used token) folds to the same neutral `invalid` (no
  oracle). Constant-time hash compare + a single conditional single-use flip transition the row
  `requested` → `email-validated`. Has its own independent per-IP rate-limit floor.
- `GET /api/v1/admin/signups[?status=&limit=]` — admin-gated (`spec-admins`). Lists signup
  requests in any state, newest first (states: `requested`, `email-validated`,
  `admin-approved`, `provisioned`, `rejected`, `expired`). Admins see the plaintext email (an
  SSE-KMS-protected attribute value); keys/logs stay hashed (`email_hash`).
- `POST /api/v1/admin/signups/{email_hash}/approve` — admin-gated. Approves ONLY from
  `email-validated` (409 otherwise), then provisions **synchronously**: mints an HA-2 invite
  (`approved=true`, email-bound) and SES-emails the join link
  (`https://spec.elasticninja.com/join?code=...`), then stamps `provisioned` (idempotent).
- `POST /api/v1/admin/signups/{email_hash}/reject` — admin-gated. Rejects from any
  non-terminal state (including a partial `requested` row); optional body `{"reason": "..."}`.
  Idempotent.

Every knob is unset by default so a local run degrades gracefully: intake still 202s (without
enqueuing), validate returns the neutral `invalid`, and the admin `/signups*` endpoints return
501 when `SIGNUPS_TABLE` is unset — mirroring the invites 501 contract. Infra
(`infra/terraform/signups.tf` + `signup_worker_lambda/`): a dedicated `${name_prefix}-signups`
DynamoDB table (SSE-KMS, PITR, TTL, a `GSI1` status index), a `${name_prefix}-signup-ratelimit`
counter table, an SQS intake queue + DLQ, and the signup worker Lambda (Cognito `ListUsers`,
writes the `requested` row, SES's the magic link storing only the token hash) — least-privilege
IAM scoped to exact ARNs, reusing the HA-6 SES send policy + configuration set. See
`AGENTS_API.md` → "Public signup queue" for full request/response shapes and examples.

**Deferred, not shipped:** an S3 WORM audit bucket and peppered ip/ua fingerprints — documented as
a follow-up, tracked separately from this backend + infra slice.

## Backups

Data lives in the Docker `pgdata` volume (survives restarts/reboots, destroyed by
`docker compose down -v`). Snapshot it any time, or schedule a daily dump:

```bash
scripts/backup.sh                      # one-off -> backups/specserver-<ts>.sql.gz (keeps newest 14)
scripts/install-backup-schedule.sh     # daily 03:00 via launchd
# restore: gunzip -c backups/specserver-latest.sql.gz | docker compose exec -T db psql -U spec -d specserver
```

## Status

All planned epics are shipped and tested (**68 passing** on Postgres; **110 passed, 3 skipped**
running the cross-backend suite against Postgres + DynamoDB Local): MVP, `PORT` (SPEC.md
round-trip), `LOG` (events + decisions + chain tracking), `HARDEN` (Alembic, lease reaper,
idempotency, pagination), `DOGFOOD` — this server now hosts **its own** backlog — and `SLS`
(pluggable storage: a DynamoDB adapter with the same atomic-claim/atomic-reservation/
optimistic-lock guarantees as Postgres, behind `STORAGE_BACKEND`). The current backlog lives on
the running server (project slug `spec-server`); `SPEC.md` is its readable mirror.

This repo is itself developed with the SPEC-driven multi-agent workflow it hosts — see `CLAUDE.md`
and `.claude/agents/`. To adopt it in another repo, see `INTEGRATION_GUIDE.md`.
