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
to require a Cognito RS256 JWT with a per-request scope (`tasks.read`/`tasks.write`/
`projects.admin`), or set `API_KEYS` for the simpler legacy static-bearer-token mode — Cognito
takes precedence if both are set. **CORS is off by default** too; set `CORS_ORIGINS` (an
exact-match allow-list, never `*`) to let the dashboard call the API from a browser. See
`AGENTS_API.md` → "Authentication" for the full precedence ladder, the scope table, and how to
mint a token, and `.env.example` for every knob.

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
  auto-emitted on claim/complete/reserve.
- **Chain-run tracking** — record a task's pass through the mandated agent chain; a skipped step
  needs a justification.
- **Idempotency-Key** replay on `claim-next`/`reserve`; **lease reaper** (abandoned tasks become
  re-claimable); **pagination** on list endpoints.
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
| REST blueprints (projects · agents · epics · tasks · reservations · ports · log · chains) | `app/blueprints/` |
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
| `DYNAMODB_ENDPOINT_URL` | _(none)_ | Override endpoint for the `dynamodb` backend, e.g. `http://dynamodb-local:8000` for DynamoDB Local. Unset ⇒ boto3 talks to real AWS. |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | _(none)_ | Standard AWS/boto3 credential env vars, used only by the `dynamodb` backend. |
| `LEASE_DEFAULT_TTL` | `1800` | Claimed-task lease seconds |
| `API_KEYS` | _(empty)_ | Comma-separated bearer tokens (legacy static auth). Empty ⇒ auth off (local-only). Ignored if `COGNITO_ISSUER` is set. |
| `COGNITO_ISSUER` | _(empty)_ | OIDC issuer for Cognito RS256 JWT auth (AUTH-2). When set, takes precedence over `API_KEYS`. See `AGENTS_API.md` → "Authentication" and `.env.example` for the full `COGNITO_*`/`JWKS_*`/`AUTH_SCOPE_*` knob set. |
| `CORS_ORIGINS` | _(empty)_ | Comma-separated exact-match browser-origin allow-list for the dashboard (AUTH-7). Empty ⇒ CORS off. `*` is never honoured. |

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
