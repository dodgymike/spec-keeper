# Spec Server — Specification

> Checkbox legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` superseded/cancelled.

This file is the source of truth for the Spec Server's own development **until the `DOGFOOD`
epic migrates task management onto the running server itself**. After that, the backlog lives in
the server (project slug `spec-server`) and this file is regenerated as a readable mirror.

Work proceeds **one claimed task at a time** through the mandated chain
**spec-keeper → implementer → test-engineer → reviewer → security → documentation**
(skipping a step requires a one-line justification in `AGENT_LOG.md`).

---

## In Progress

_(none)_

---

## Completed

### EPIC MVP — Minimal viable spec server (shipped 2026-06-30)

Decision DEC-1: isolation model is a **single shared backlog per project with per-task
owner + lease** (not workspace-per-agent). Decision DEC-2: **MVP first**, deferring
logs/decisions/chain-tracking/import-export to phase 2+.

- [x] **MVP-1 · Scaffold the Flask project** (BE). `app/` package, `create_app()` factory,
  `requirements.txt`, config from env. _Proof: `python -c "from app import create_app; create_app()"`
  imports cleanly; container boots._
- [x] **MVP-2 · PostgreSQL schema + SQLAlchemy models** (BE). projects, agents, epics, tasks
  (status enum, priority, component, owner, lease, version), tags, task_relations, commit_refs,
  counters, reservations, leases — with the collision-proof `UNIQUE(project_id, namespace, value)`
  and partial-unique `one_active_lease`. _Proof: `flask init-db` creates all tables; pytest schema
  round-trips._
- [x] **MVP-3 · REST API blueprints** (BE). Projects/agents/epics CRUD; tasks CRUD + `claim-next` +
  `complete` + `release` + `status` + `relations` + `commits`; reservations + counters. Optimistic
  locking via `version`/`If-Match` → 412. _Proof: end-to-end curl flow (create→claim→reserve→
  complete) returns expected statuses; `If-Match "v99"` → 412._
- [x] **MVP-4 · OpenAPI + Swagger UI** (BE). flask-smorest auto-generates OpenAPI 3 from the
  Marshmallow schemas; served at `/openapi.json`, Swagger UI at `/docs`. _Proof:
  `curl /openapi.json | jq .openapi` == "3.0.3" with all resource paths present._
- [x] **MVP-5 · Docker + docker-compose (Flask + Postgres)** (infra). Postgres + gunicorn app,
  healthchecks, entrypoint waits for DB then `flask init-db`. _Proof: `docker compose up -d` →
  `curl /readyz` == ready._
- [x] **MVP-6 · Concurrency tests** (BE). pytest proving: claim picks one + priority order,
  concurrent claims never collide (8 threads → 8 distinct), reservation collision-proof (20 threads →
  20 distinct contiguous), complete flips to done, `If-Match` 412. _Proof: `pytest -q` → 15 passed._
- [x] **MVP-7 · Self-hosting agent config** (docs). `CLAUDE.md`, `.claude/agents/*` (the proven
  chain, repointed to the API), `SPEC.md`, `README.md`, `AGENTS_API.md`. _Proof: this file + the
  agent roster exist and describe the API-driven workflow._

### EPIC PORT — SPEC.md round-trip / migration bridge (shipped 2026-06-30)

Import an existing `SPEC.md` into the DB and render the DB back to a `SPEC.md`, so a repo can run
file-and-server in parallel before going server-only. Implemented in `app/specmd.py` +
`app/blueprints/ports.py`. Validated against the real 568-line feed-reader `SPEC.md` (43 tasks).

- [x] **PORT-1 · Parser: `SPEC.md` → structured tasks** (BE). Checkbox states `[ ] [~] [x] [-]`,
  epic-scoped IDs, inline `(component, priority, status)` metadata, `_Proof:_` lines, continuation
  lines. _Proof: `pytest -k parse`._
- [x] **PORT-2 · `POST /projects/{slug}/import`** (BE). Idempotent upsert keyed on task key.
  _Proof: importing the same file twice yields 0 new tasks._
- [x] **PORT-3 · Renderer: DB → `SPEC.md`** (BE). Canonical render grouped by section → epic → task.
  _Proof: export contains `- [x] FOUND-1 · …`._
- [x] **PORT-4 · Round-trip fidelity** (BE). `parse(render(parse(x)))` is stable. _Proof:
  `pytest -k roundtrip`._
- [x] **PORT-5 · `POST /export/diff` dry-run** (BE). Reports added/removed/changed vs a posted file.
  _Proof: a single flipped task shows as `1 changed`._

### EPIC LOG — Append-only log + decisions (shipped 2026-06-30)

`app/models.py` (Event, Decision) + `app/blueprints/log.py`. Events auto-emitted on
claim/complete/reserve/decision.

- [x] **LOG-1 · `events` table + `/events` endpoints** (BE). Append-only stream (replaces
  `AGENT_LOG.md`); filter by task/agent/type, paginated. _Proof: `pytest -k event` — claim/complete/
  reserve emit events; manual note round-trips._
- [x] **LOG-2 · `decisions` table + `/decisions` endpoints** (BE). ADR-style records (replaces
  `DECISIONS.md`); recording one also emits an event. _Proof: `pytest -k decision`._

- [x] **LOG-3 · Chain-run + step tracking** (BE). `ChainRun`/`ChainStep` models +
  `app/blueprints/chains.py`. Start a run per task; PUT steps; a skipped step without a
  justification is rejected. _Proof: `pytest -k chain` — skip without justification → 422._

### EPIC HARDEN — Production-readiness (shipped 2026-06-30)

- [x] **HARDEN-1 · Alembic migrations** (BE). `create_all` replaced by a versioned baseline
  migration (`migrations/`, autogenerated from the final models); the entrypoint stamps a legacy
  create_all DB then `alembic upgrade head`. _Proof: upgrade head on an empty DB builds 17 tables;
  downgrade to base works._
- [x] **HARDEN-2 · Lease expiry reaper** (BE). An abandoned (expired-lease) in_progress task is
  reclaimed by the next `claim-next`; the stale lease is retired so one-active-lease holds. _Proof:
  `pytest -k expired_lease`._
- [x] **HARDEN-3 · Idempotency-Key on claim/reserve** (BE). `app/idempotency.py`; a retried POST
  with the same `Idempotency-Key` replays the stored response instead of allocating again. _Proof:
  `pytest -k idempotent` — same key twice → one allocation._ Follow-up: the rare concurrent
  same-key race returns the live (rolled-back) object rather than the stored replay; sequential
  retries (the real case) are correct.
- [x] **HARDEN-4 · Pagination (`limit`/`offset`) on task + event lists** (BE). _Proof:
  `pytest -k pagination`._

---

## To Do

### EPIC DOGFOOD — Migrate the server onto itself

- [ ] **DOGFOOD-1 · Create the `spec-server` project + agent registry via the API** (ops). _Proof:
  `POST /projects` → 201; agents registered._
- [ ] **DOGFOOD-2 · Import this repo's own `SPEC.md`** (ops, needs PORT-2). _Proof:
  `GET /projects/spec-server/tasks?status=todo` count matches the remaining checkboxes here._
- [ ] **DOGFOOD-3 · Repoint `.claude/agents/spec-keeper.md` to the API as the source of truth**
  (docs). _Proof: a real task is claimed → completed end-to-end via the API._
- [ ] **DOGFOOD-4 · CI round-trip gate** (infra, needs PORT-4). GitHub Action: `docker compose up`,
  run pytest + the import/export round-trip. _Proof: the Action is green._

---

## Conventions

- **Branch:** feature branches off `main`; do not commit to `main` without asking.
- **Mandated chain:** spec-keeper → implementer → reviewer → security at minimum; justify any skip in
  `AGENT_LOG.md`.
- **Reserved identifiers:** reserve migration/table/queue numbers via `POST /reservations` before
  use — never choose one independently.
- **One task at a time:** claim via `claim-next`; never start unclaimed work.
- **No secrets in tracked files:** `.env` is gitignored; `.env.example` documents the knobs.
- **Tests need real Postgres:** point `TEST_DATABASE_URL` at a throwaway DB.
