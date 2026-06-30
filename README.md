# Spec Server

A local, concurrency-safe **task/spec management server for AI coding agents**. It replaces the
fragile flat-`SPEC.md` workflow (manually picking the next unchecked box, hand-reserving migration
numbers, append-only file locking) with a small REST API backed by PostgreSQL.

Built with **Python + Flask (flask-smorest) + SQLAlchemy + PostgreSQL**, runs in **Docker**, and
auto-publishes an **OpenAPI 3** contract that agents consume directly.

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

## What's included

- **Tasks** — CRUD, atomic `claim-next`, `complete`, `release`, `status`, relations, commit refs;
  per-task `owner`/lease; optimistic locking (`version`/`If-Match` → 412).
- **Atomic number reservation** — collision-proof per-namespace counters.
- **Per-project agent registry** — each project has its own roster (two projects can both have a
  `spec-keeper`).
- **SPEC.md round-trip** — import an existing `SPEC.md`, export the DB back to one (`app/specmd.py` +
  `blueprints/ports.py`); the migration bridge.
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
| Idempotency-Key store | `app/idempotency.py` |
| `SPEC.md` import/export parser + renderer | `app/specmd.py` |
| REST blueprints (projects · agents · epics · tasks · reservations · ports · log · chains) | `app/blueprints/` |
| Alembic migrations (run on boot) | `migrations/` |
| Tests (concurrency + round-trip + idempotency) | `tests/` |
| Backup / migrate / schedule scripts | `scripts/` |

Key tables: `projects`, `agents` (project-scoped), `epics`, `tasks` (status enum, priority,
component, `owner`, `version`, lease, `section`), `tags`, `task_relations`, `commit_refs`,
`counters` + `reservations` (atomic numbering), `leases` (one active per task), `events`,
`decisions`, `chain_runs` + `chain_steps`, `idempotency_keys`.

## Running the tests

The guarantees are Postgres-specific (skip-locked dequeue, on-conflict upsert, partial unique
indexes), so tests run against a real PostgreSQL:

```bash
docker compose exec db psql -U spec -d specserver -c "CREATE DATABASE specserver_test;"
docker compose exec -T -e TEST_DATABASE_URL=postgresql+psycopg://spec:spec@db:5432/specserver_test \
  app python -m pytest -q
# -> 35 passed
```

On boot the container runs `alembic upgrade head` (adopting a legacy `create_all` database by
stamping it first); the test suite builds its schema directly from the models.

## Configuration (`.env`)

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://spec:spec@db:5432/specserver` | SQLAlchemy connection |
| `LEASE_DEFAULT_TTL` | `1800` | Claimed-task lease seconds |
| `API_KEYS` | _(empty)_ | Comma-separated bearer tokens. Empty ⇒ auth off (local-only). |

## Backups

Data lives in the Docker `pgdata` volume (survives restarts/reboots, destroyed by
`docker compose down -v`). Snapshot it any time, or schedule a daily dump:

```bash
scripts/backup.sh                      # one-off -> backups/specserver-<ts>.sql.gz (keeps newest 14)
scripts/install-backup-schedule.sh     # daily 03:00 via launchd
# restore: gunzip -c backups/specserver-latest.sql.gz | docker compose exec -T db psql -U spec -d specserver
```

## Status

All planned epics are shipped and tested (**35 passing**): MVP, `PORT` (SPEC.md round-trip), `LOG`
(events + decisions + chain tracking), `HARDEN` (Alembic, lease reaper, idempotency, pagination),
and `DOGFOOD` — this server now hosts **its own** backlog. The current backlog lives on the running
server (project slug `spec-server`); `SPEC.md` is its readable mirror.

This repo is itself developed with the SPEC-driven multi-agent workflow it hosts — see `CLAUDE.md`
and `.claude/agents/`. To adopt it in another repo, see `INTEGRATION_GUIDE.md`.
