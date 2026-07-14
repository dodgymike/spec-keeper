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

## DEC-4 — 2026-07-02 — Push-only Jira sync (no pull, no bidirectional)

**Context.** Options: (a) bidirectional sync (Jira webhooks pull changes back), (b) pull-only
(poll Jira for updates), (c) push-only (Spec Server is source of truth, pushes to Jira as a
mirror). Bidirectional requires webhook infrastructure, conflict resolution, and introduces a
second source of truth. Pull requires polling infrastructure and eventual-consistency headaches.

**Decision.** Adopt **(c): push-only** — the Spec Server pushes to Jira Cloud on task lifecycle
events. Jira is a read-only mirror for stakeholders who live in Jira; the server remains the sole
source of truth for task state.

**Consequences.** Manual changes in Jira are not reflected back. If bidirectional sync is later
needed, webhooks can be layered on top without changing the existing push path.

## DEC-5 — 2026-07-02 — Sync triggers on create + complete only (not every status change)

**Context.** Task lifecycle has many status transitions (todo, in_progress, blocked, deferred,
done, superseded, cancelled). Mapping each to a Jira transition is complex, fragile (Jira
workflows vary), and low-value for the primary use case (visibility for stakeholders).

**Decision.** Sync fires on **task create** (creates a Jira issue) and **task complete**
(transitions the Jira issue to "Done") only. Other status changes are not pushed.

**Consequences.** Jira issues show "created" and "done" — sufficient for stakeholder visibility.
Extending to all status changes is deferred as JIRA-14.

## DEC-6 — 2026-07-02 — Best-effort inline sync with error-flag-and-retry

**Context.** Options: (a) blocking sync (fail the API call if Jira is down), (b) background queue
(async workers process sync jobs), (c) best-effort inline with stored error and manual retry
endpoint. A background queue adds infrastructure (Redis/Celery/etc.) for a feature that is
non-critical — Jira sync is a convenience mirror, not a correctness requirement.

**Decision.** Adopt **(c): best-effort inline**. The sync functions (`sync_task_created`,
`sync_task_completed`) never raise — any failure is stored on `task.jira_sync_error` and emitted
as a `jira_sync_error` event. The `POST /projects/{slug}/jira/sync` retry endpoint lets operators
re-attempt all failed syncs in bulk.

**Consequences.** Zero additional infrastructure. Task creation/completion always succeeds
regardless of Jira availability. Trade-off: no automatic retry — operators must trigger retries
manually or via a scheduled cron.

## DEC-7 — 2026-07-02 — Per-project DB-backed Jira config with Fernet-encrypted tokens

**Context.** Options: (a) global env vars for one Jira instance, (b) per-project DB rows. The
server manages multiple projects; each may point at a different Jira instance/project. Storing
credentials in env vars doesn't scale to multi-project and makes it impossible to
enable/disable per project at runtime.

**Decision.** Store Jira config **per-project in the database** (`jira_project_config` table).
API tokens are **Fernet-encrypted at rest** using the `JIRA_TOKEN_ENCRYPTION_KEY` env var; decrypted
in-memory only at call time. The encryption key is the only Jira-related env var.

**Consequences.** Each project independently configures and toggles Jira sync. Rotating the
encryption key requires re-encrypting stored tokens (a migration script). The token never appears
in API responses (only `has_token: true/false`).

## DEC-8 — 2026-07-02 — Transition cache with refresh-once-before-failing semantics

**Context.** Transitioning a Jira issue to "Done" requires the transition/status ID, which varies
per Jira project workflow. Options: (a) hardcode IDs, (b) fetch per-issue transitions at call time,
(c) cache project-wide statuses and refresh on miss.

**Decision.** Adopt **(c): transition cache**. Project statuses are fetched from Jira's
`GET /project/{key}/statuses` endpoint and stored in `jira_project_config.cached_transitions`
(JSONB). The cache is warmed on config save (when `enabled=true`). At sync time, `find_transition`
does a case-insensitive lookup; on cache miss it refreshes exactly once, then fails with
`TransitionNotFoundError` if still missing.

**Consequences.** Minimizes Jira API calls (one call per config save + at most one retry per
unknown transition name). Handles Jira workflow changes gracefully (the single refresh picks up
new statuses). Statuses deduplicated by ID across issue types.
