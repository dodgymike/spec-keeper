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

---

## To Do

### EPIC PORT — SPEC.md round-trip (the migration bridge)

> Goal: incremental adoption. Import an existing `SPEC.md` into the DB and render the DB back to a
> `SPEC.md`, so a repo can run file-and-server in parallel before going server-only.

- [ ] **PORT-1 · Parser: `SPEC.md` → structured tasks** (BE). Parse sections → epics → phases →
  tasks; checkbox states, epic-scoped IDs, inline metadata (component, priority, status keywords),
  `_Proof:_` lines, commit SHAs, test summaries. _Proof: `pytest -k parse` on a fixture yields the
  expected task count + statuses._
- [ ] **PORT-2 · `POST /projects/{slug}/import`** (BE). Idempotent upsert of the parsed tree keyed on
  task key. _Proof: importing the same file twice yields no duplicate tasks._
- [ ] **PORT-3 · Renderer: DB → `SPEC.md`** (BE). Group section → epic → phase → task; reconstruct
  checkbox states and metadata. _Proof: render contains `- [x] MVP-1` for a completed task._
- [ ] **PORT-4 · Round-trip fidelity** (BE). `import(export(import(fixture)))` is stable. _Proof:
  `pytest -k roundtrip` — re-imported export yields an identical normalized tree._
- [ ] **PORT-5 · `GET /export/diff` dry-run** (BE). Show what export would change vs a posted
  `SPEC.md` (adoption safety). _Proof: a changed task shows in the diff, an unchanged one does not._

### EPIC LOG — Append-only log, decisions, chain tracking

- [ ] **LOG-1 · `events` table + `/events` endpoints** (BE). Append-only agent-log stream (replaces
  `AGENT_LOG.md`); filter by task/agent/type. _Proof: POST event → GET stream contains it; UPDATE on
  events is rejected._
- [ ] **LOG-2 · `decisions` table + `/decisions` endpoints** (BE). ADR-style records (replaces
  `DECISIONS.md`). _Proof: POST decision → GET returns title._
- [ ] **LOG-3 · Chain-run + step tracking** (BE). Track the mandated chain per task; a skipped step
  requires a justification. _Proof: skip without justification → 422._

### EPIC HARDEN — Production-readiness

- [ ] **HARDEN-1 · Alembic migrations** (BE). Replace `create_all` with versioned migrations
  (enums, partial indexes, a `version`-bump trigger as defence-in-depth). _Proof: `alembic upgrade
  head` on an empty DB builds the full schema; downgrade works._
- [ ] **HARDEN-2 · Lease expiry reaper** (BE). A claimed task whose lease expired becomes claimable
  again. _Proof: a test with a 0s TTL re-claims an abandoned task._
- [ ] **HARDEN-3 · Idempotency-Key on claim/reserve** (BE). A retried POST after a network blip does
  not double-allocate. _Proof: same key twice → one allocation._
- [ ] **HARDEN-4 · Pagination (`limit`/`cursor`) on all list endpoints** (BE). _Proof: list with a
  small page size returns a cursor that fetches the next page._

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
