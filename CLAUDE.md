# Development Protocol

This project (the **Spec Server**) is itself built with the SPEC-driven, multi-agent
workflow it is designed to host. It bootstraps on `SPEC.md`, then migrates its own task
management to the running server (dogfooding).

Always follow the spec (`SPEC.md` for now; the Spec Server API once migrated — see
"Source of truth" below).
Always use your agents when changing code: planner → spec-keeper → implementer →
test-engineer → reviewer → security → documentation

Agent roster (`.claude/agents/`):
- **planner** — breaks large requests into an atomic, ordered implementation plan.
- **spec-keeper** — owns the task backlog; breaks work into atomic tasks and tracks status.
  It is the ONLY agent that mutates task state (via the Spec Server API, or `SPEC.md` pre-migration).
- **implementer** — writes the code for exactly one task.
- **test-engineer** — writes/improves automated tests and runs the narrowest check.
- **reviewer** — checks correctness, scope, and that exactly one task was done.
- **security** — audits for vulnerabilities, injection, and leaked secrets.
- **documentation** — updates README, `AGENTS_API.md`, and inline docs.
- **feature-runner** — runs ONE task end-to-end through the mandated chain, code-only and
  parallel-safe. Use this INSTEAD OF a generic agent for any change touching app code.

Extended roster (adapted from the sibling bird-viz project for the AWS-deploy + UI + serverless
work — see the `AGENTS`, `SLS`, `AUTH`, `INFRA`, `UI` epics):
- **architecture-reviewer** — reviews component boundaries, the DynamoDB/Postgres data model, and the
  concurrency invariants. Read-only.
- **data-reviewer** — reviews the pluggable storage layer (Postgres + DynamoDB adapters), schema/
  migration discipline, key design, and adapter parity. Read-only.
- **performance-reviewer** — reviews latency hot paths, cold starts, query efficiency, and per-request
  cost. Read-only.
- **reliability-reviewer** — reviews failure modes, idempotency, multi-item atomicity, and the
  invariants under failure. Read-only.
- **ui-reviewer** — reviews the React/Vite dashboard's UI/UX, a11y, CSP-cleanliness, and copy.
  Read-only.
- **aws-infra** — the ONLY role that mutates AWS infra (Terraform durable, CLI transient); serverless
  stack (Lambda, DynamoDB, API Gateway, Cognito, CloudFront). Cost- and teardown-aware.
- **aws-cost-optimizer** — advisory: hunts AWS cost (Lambda/DynamoDB/logs/orphans). Read-only.
- **aws-teardown-enforcer** — owns the preview-env reaper; guarantees transient infra is torn down.
- **deploy-coordinator** — the ONLY role that deploys; runs the single coordinated deploy wave +
  mandatory unauthenticated-route smoke check.
- **deep-diver** — investigates a hard "why is X broken / how to build Y" question → a `_DEEPDIVE.md`.
- **report-writer** — maintains the self-contained HTML report (`report/report.html`), one tab per
  completed task, assembled from the task's Spec Server note journal.

Skill (`.claude/skills/`): **presentation-slides** — 16:9 matplotlib slides explaining the system.

For ANY code change the chain **spec-keeper → implementer → reviewer → security** is
MANDATORY; skipping a step requires an explicit one-line justification in `AGENT_LOG.md`.
For **UI** changes add **ui-reviewer**; for **AWS infra** changes route through **aws-infra** (mutate)
and **deploy-coordinator** (deploy) — never `terraform apply` from a generic agent.

## Source of truth

- **Pre-migration (now):** `SPEC.md` at the repo root is the single source of truth.
- **Post-migration (the `DOGFOOD` epic):** the running Spec Server is the source of truth.
  The backlog lives in the database under project slug `spec-server`; `SPEC.md` is regenerated
  from the server as a readable mirror. spec-keeper talks to the API instead of editing the file.

The whole point of this server is to replace fragile flat-file task management. The two
hard problems it solves — and which every agent must rely on rather than work around:

1. **Atomically claim exactly one task.** Never scan a file and "pick the next unchecked box"
   by hand — two agents racing both pick the same one. Call
   `POST /projects/{slug}/tasks/claim-next` with your agent slug; the server hands each caller
   a distinct task (`FOR UPDATE SKIP LOCKED`) or 204 when the backlog is dry.
2. **Reserve numbered resources atomically.** Never choose a migration/table/queue number by
   reading the max and adding one — that is exactly how "two agents both grabbed 024" happens.
   Call `POST /projects/{slug}/reservations` with a `namespace`; the server returns a unique,
   monotonically increasing value (`INSERT ... ON CONFLICT DO UPDATE RETURNING`).

See `AGENTS_API.md` for the full recipe book and `README.md` for how to run the server.

## Each agent keeps its specs separate

Isolation model: a **single shared backlog per project**, with per-task **ownership**.
- When you claim a task, the server stamps it with your agent slug (`owner`) and a lease.
- "My specs" = `GET /projects/{slug}/tasks?owner=<me>`.
- Two agents never hold the same task: claim-next skips locked rows, and a partial unique index
  permits only one active lease per task.
- Hand off by releasing (`POST .../release`) or completing (`POST .../complete`).

## Work in atomic increments

1. Read the spec (claim a task via the API; pre-migration, read `SPEC.md`).
2. **Claim exactly one task** (`claim-next`) — never start work you didn't claim.
3. Restate the task in one sentence.
4. Make the smallest code change that completes only that task.
5. Run the narrowest relevant check (the affected `tests/test_*.py`, or `pytest -k <area>`).
6. Commit with a descriptive message + short tldr.
   - Work on a feature branch; never commit directly to `main` without asking.
7. **Complete the task** (`POST .../complete` with `commit_sha`, `test_summary`, `proof_cmd`) —
   this is the "flip the checkbox to [x]" operation. Add any discovered follow-up tasks
   (`POST .../tasks`). Pre-migration, mark `SPEC.md` instead.
8. Record decisions in `DECISIONS.md` if any were made.
9. Append an entry to `AGENT_LOG.md`.
10. **Tidy-up & git hygiene (definition-of-done — a task is NOT complete until ALL hold):**
    - `git status --porcelain` is EMPTY (clean tree). Every created/changed file is committed
      or covered by `.gitignore`. New files MUST be `git add`ed.
    - No scratch in the repo: temp goes under `/tmp` or an ignored `/scratch/`, never tracked.
    - The task is actually marked done in the backlog (server `status=done`, or `[x]` in `SPEC.md`)
      — not merely "suggested".
    - One logical commit per task, footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
    - The mandated chain actually ran: reviewer AND security were invoked (or a recorded
      justification for skipping). A deferred `[SECURITY-REVIEW]` tag is NOT a substitute.
11. Stop and report: files changed · test result · `git status` is clean · next recommended task.

Do not batch unrelated tasks. Do not refactor unless the task explicitly asks for it.
If the spec is wrong or incomplete, fix the spec first, then continue.
A task is not complete until all documentation is updated.

For actions needing permission multiple times, write a script and ask permission once.

## What this project is

A local **Flask + PostgreSQL** service (run via `docker-compose`) that exposes a concurrency-safe
REST API for managing task backlogs ("specs") for AI coding agents. It auto-generates an
**OpenAPI 3** document (served at `/openapi.json`, Swagger UI at `/docs`) that agents consume as
their interface contract.

Layout:
- `app/__init__.py` — the `create_app()` factory and CLI (`flask init-db`).
- `app/models.py` — SQLAlchemy schema (projects, agents, epics, tasks, tags, relations,
  commit refs, counters, reservations, leases).
- `app/schemas.py` — Marshmallow schemas; the single source of truth for validation AND OpenAPI.
- `app/services.py` — the two atomic operations: `claim_next_task` (skip-locked dequeue) and
  `reserve_number` (on-conflict upsert).
- `app/blueprints/` — one flask-smorest Blueprint per resource (projects, agents, epics, tasks,
  reservations) plus plain health probes.
- `tests/` — pytest, including the concurrency tests that prove no-collision claiming and
  collision-proof reservation. **Tests require a real PostgreSQL** (the guarantees are
  Postgres-specific): point `TEST_DATABASE_URL` at a throwaway database.

## Commands

- Run the stack: `docker compose up -d --build` → API at `http://localhost:8080`.
- Health: `curl localhost:8080/readyz`. OpenAPI: `curl localhost:8080/openapi.json`. Docs: `/docs`.
- Create the schema (idempotent): `flask init-db` (the container entrypoint runs this on boot).
- Tests (in-container, isolated DB):
  `docker compose exec -T -e TEST_DATABASE_URL=postgresql+psycopg://spec:spec@db:5432/specserver_test app python -m pytest -q`
  (create `specserver_test` once: `docker compose exec db psql -U spec -d specserver -c "CREATE DATABASE specserver_test;"`).

## Concurrency invariants (do not regress these)

Three guarantees hold on **both** backends (`STORAGE_BACKEND=postgres|dynamodb`) with identical
observable behaviour. Each is realised by a backend-specific primitive — the Postgres reference and
its DynamoDB equivalent are listed side by side. Never downgrade either to a read-then-write.
See `STORAGE_ABSTRACTION_DEEPDIVE.md` §4 for the full mapping and the "Backend parity (hard rule)"
section below for why divergence is a bug, not a "backend difference".

- **Optimistic locking on tasks.** `tasks.version` is the ETag; `If-Match: "v<n>"` on a mutation
  returns **412** on mismatch, and every task mutation increments `version`.
  - *Postgres:* the `version` column compared in the `UPDATE ... WHERE version = :expected`.
  - *DynamoDB:* a `ConditionExpression version = :expected` on the guarded `PutItem`/`UpdateItem`;
    a `ConditionalCheckFailedException` → `VersionConflict` → 412.
- **Atomic claim (exactly one winner).** `claim-next` hands each of N racing callers a distinct task.
  - *Postgres:* `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`, then update.
  - *DynamoDB:* a GSI1 candidate `Query` (status+priority) + a conditional `UpdateItem`
    (`attribute_not_exists(owner) AND status = todo`), retrying the next candidate on
    `ConditionalCheckFailed`. Two racers never both win.
- **Atomic reservation (collision-proof numbering).** `reserve_number` returns a unique,
  monotonically increasing value per `(project, namespace)`.
  - *Postgres:* `INSERT ... ON CONFLICT (project_id, namespace) DO UPDATE SET current_value =
    current_value + 1 RETURNING`, backstopped by `UNIQUE(project_id, namespace, value)`.
  - *DynamoDB:* a per-item atomic `ADD current_value :1` on the counter item, backstopped by a
    conditional `PutItem` on a `UNIQUE(namespace, value)` guard item.
  - Do not replace either with read-max-plus-one.

**Multi-item atomicity:** where Postgres relies on a single transaction to write several rows at
once — `complete` (task + commit + lease + event), `supersedes` (relation + destination-task flip) —
the DynamoDB adapter uses `TransactWriteItems` to keep the write all-or-nothing. Reservation's
audit-row + counter + event likewise commit in one `TransactWriteItems`.

## Backend parity (hard rule)

The storage backend is **switchable** (`STORAGE_BACKEND=postgres|dynamodb`), and the two backends
MUST always have **feature parity and identical observable behaviour**. This is non-negotiable:

- Every access pattern and every guarantee above (atomic claim, collision-proof reservation,
  optimistic locking / 412, lease semantics, idempotency, notes/relations/commits, import/export)
  must behave the **same** on both backends — same inputs → same outputs, same status codes, same
  error semantics. A behaviour that holds on one backend but not the other is a **bug**, not a
  backend "difference".
- **No feature lands on one backend only.** If you add or change anything in the storage layer,
  implement it in BOTH adapters (Postgres reference + DynamoDB) in the same task, or the task is
  not done.
- The **adapter-parity test suite** (SLS-8) is the enforcement mechanism: the concurrency and
  behaviour tests run against BOTH backends (Postgres + DynamoDB Local) and must pass on both.
  A change that can't pass on both backends is not mergeable — fix the adapter, don't skip the test.
- `data-reviewer` and `reliability-reviewer` explicitly check adapter parity; any divergence is at
  least a P1 finding.

## Secrets & safety (hard rules)

- Never commit real secrets. `.env` is gitignored; `.env.example` documents the knobs.
- The optional `API_KEYS` bearer auth is for shared deployments; the default (empty) is local-only.
- **Cognito M2M client secrets live in AWS Secrets Manager** (referenced by ARN — see
  `infra/terraform/cognito.tf` output `cognito_m2m_secret_arns`), **never** in the repo, `*.tfvars`,
  terraform outputs, or git. Grant each agent's role `secretsmanager:GetSecretValue` on its own ARN.
- **The server holds no secret at rest for JWT auth** — it validates tokens against Cognito's public
  JWKS (`COGNITO_JWKS_URI`). Only agents (clients) hold credentials, to mint tokens via
  `scripts/agent_token.py` (which never prints/logs the secret or token). See README → "Secrets &
  tokens" and `AGENTS_API.md` → "Authenticating to the deployed server".
- SQL must stay parameterized (SQLAlchemy core / bound params) — never string-format user input
  into SQL. The security agent flags any raw f-string SQL with user data. (The DynamoDB adapter
  applies the same rule: values bind via `ExpressionAttributeValues`, never formatted into an
  expression string.)

## Parallel-agent coordination

The server IS the coordination layer — this replaces the old "append-only shared file, one writer
at a time" convention:
- Claim work with `claim-next` (no two agents get the same task).
- Reserve shared identifiers with `POST /reservations` (no two agents get the same number).
- Keep your in-flight specs separate via the `owner` field; promote/hand off by release/complete.
- `DECISIONS.md` and `AGENT_LOG.md` remain append-only local files until the server grows
  first-class decision/event endpoints (a Phase 2 task in `SPEC.md`).
