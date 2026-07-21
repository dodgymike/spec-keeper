# Decisions

Append-only. New decisions supersede old ones via a new dated entry; never rewrite history.

## DEC-1 — 2026-06-30 — Isolation model: shared backlog + per-task owner/lease

**Context.** Agents need to "keep their specs separate from the rest". Options considered:
(a) workspace/branch lane per agent with promote-to-shared, (b) a single shared backlog per project
where each task carries an `owner` and a lease, (c) a separate project per agent.

**Decision.** Adopt **(b): one shared backlog per project, with per-task ownership and a lease**.
"My specs" is `GET /tasks?owner=<me>`. Claiming a task stamps the owner and an exclusive lease; a
partial unique index allows only one active lease per task.

**Consequences.** Simplest schema that still prevents collisions; no workspace table in the MVP.
Cross-agent coordination (shared migration-number reservation, blocks/supersedes relations) stays
trivial because everything is in one project. If stronger drafting privacy is later required, a
`workspace_id` column can be added without reworking the lease/claim machinery.

## DEC-2 — 2026-06-30 — MVP first; defer logs/decisions/chain/round-trip

**Context.** The full design includes append-only event/decision endpoints, chain-run tracking, and
`SPEC.md` import/export round-trip. Building all of it before anything runs is high-risk.

**Decision.** Ship an **MVP** first: projects, agents, epics, tasks (CRUD + atomic `claim-next` +
`complete`), atomic number reservation, optimistic locking, OpenAPI, Docker/Postgres, and the
self-hosting agent config. Defer events/decisions/chain-tracking (`LOG` epic), import/export
(`PORT` epic), and hardening (`HARDEN` epic) to follow-up tasks in `SPEC.md`.

**Consequences.** A runnable, tested, dogfoodable core lands immediately. `DECISIONS.md` and
`AGENT_LOG.md` remain flat files until the `LOG` epic gives the server first-class endpoints.

## DEC-3 — 2026-06-30 — Tests target real PostgreSQL, not SQLite

**Context.** The correctness guarantees rely on Postgres-specific features: `FOR UPDATE SKIP
LOCKED`, `INSERT ... ON CONFLICT DO UPDATE RETURNING`, and partial unique indexes. SQLite cannot
express them, so an in-memory SQLite test suite would give false confidence.

**Decision.** Tests run against a throwaway PostgreSQL database (`TEST_DATABASE_URL`), executed
inside the app container against the compose `db` service.

**Consequences.** Tests need the stack up (or any Postgres). This is the honest trade for testing the
behaviour that actually matters.

## DEC-4 — 2026-07-21 — Switchable storage backend via a repository abstraction (Postgres reference + DynamoDB adapter)

**Context.** The server's entire value rests on Postgres-specific primitives: `FOR UPDATE SKIP
LOCKED` (atomic claim, `services.py:133-137`), `INSERT ... ON CONFLICT DO UPDATE RETURNING`
(atomic reservation, `services.py:54-66`), and a `version`/`If-Match`/412 optimistic lock
(`helpers.py:58-73`). The product now wants the backend to be SWITCHABLE — keep Postgres, add
DynamoDB as a config-selected second backend for a serverless (Lambda + on-demand) deployment.
Options: (a) leave it Postgres-only; (b) a repository/port abstraction with two adapters;
(c) an ORM dialect swap (rejected — DynamoDB is not relational and the guarantees are not
expressible as SQL).

**Decision.** Adopt **(b): a `StorageBackend` repository interface** (`app/storage/base.py`)
that both a `PostgresBackend` (reference, wraps today's SQLAlchemy code with no behaviour change)
and a `DynamoBackend` (boto3) implement, returning backend-neutral DTOs + errors
(`NotFound`/`Conflict`/`VersionConflict`). Blueprints call `current_app.storage.<method>()`
instead of `db.session`. A factory in `app/storage/__init__.py` selects the adapter from
`STORAGE_BACKEND=postgres|dynamodb` at app creation; **default stays `postgres` locally**.
DynamoDB uses a **single-table design** (`P#<slug>` partition; ~5 GSIs — claim/status, owner,
task-key, time-ordered feed, all-projects) serving every access pattern with no Scan. The three
guarantees map to conditional writes: claim = GSI candidate query + conditional `UpdateItem`
(owner-absent) with retry; reservation = atomic `ADD` + conditional-put backstop; optimistic
lock = `version` ConditionExpression → 412. Multi-item atomic ops (complete, supersedes,
reservation contiguity+audit) use `TransactWriteItems`.

**Consequences.** Instant rollback (flip `STORAGE_BACKEND=postgres`; DynamoDB is additive, no
schema migration). Requires a DTO refactor of the `*Out` schemas (they currently dump ORM
objects with lazy relationships — the load-bearing prerequisite, tracked as SLS-2.1). The
concurrency/parity test suite must run against BOTH backends (Postgres + DynamoDB Local, SLS-8).
Cost posture improves (serverless, scales to zero) at the price of GSI write amplification and a
potential hot partition on a very busy project. Full design in `STORAGE_ABSTRACTION_DEEPDIVE.md`.

## DEC-5 — App-level Cognito JWT auth: scope inference from request, lazy PyJWT, JWKS anti-DoS (AUTH-2/AUTH-7)
Context: AUTH-2 adds real Cognito RS256 JWT validation alongside the legacy static `API_KEYS`,
and AUTH-7 adds dashboard CORS, without editing the per-endpoint blueprints (owned/off-limits).
Decisions:
- **Scope is inferred inside `require_api_key()` from `request.method` + `request.blueprint`**
  (not passed per-endpoint), because every blueprint already calls `require_api_key()` with no
  arguments and the blueprints were out of scope to change. Mapping: GET/HEAD -> tasks.read;
  mutations on the `projects`/`agents` blueprints -> projects.admin; all other mutations ->
  tasks.write. Trade-off: a new blueprint whose mutations should be admin must be added to
  `_ADMIN_BLUEPRINTS`.
- **`verify_aud=False` in `jwt.decode` + a manual audience check** that accepts `aud` OR
  `client_id`, because Cognito M2M (client_credentials) access tokens carry no `aud` claim.
  Empty `COGNITO_AUDIENCE` skips the check (documented) — real deploys must set it.
- **PyJWT/cryptography imported lazily** inside the verify path so the app still imports with
  auth off or the libs absent (preserves the 42-test auth-off baseline).
- **JWKS anti-DoS:** unknown-kid refetches are bounded by `JWKS_MIN_REFRESH_INTERVAL` (30s
  default) so a flood of bogus-kid tokens cannot amplify into a flood of outbound JWKS fetches
  (raised as P2 by both reviewer and security; fixed with a regression test).
- **CORS is exact-match only, never `*`**, because the API is used with `Authorization`
  credentials and wildcard-with-credentials is unsafe/forbidden.
