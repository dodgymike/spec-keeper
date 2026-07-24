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

## AUTH-10 — group-based authz + agent user-auth (2026-07-21)
- **Authz keys off Cognito GROUP membership, not resource-server scopes.** The verified access
  token's `cognito:groups` claim drives a permission set (spec-admins=read+write+admin,
  spec-writers=read+write, spec-readers=read), unioned across a user's groups. Rationale: the M2M
  `client_credentials` clients (one per scope profile) cost ~$6/mo each and were retired; users +
  groups are free and map cleanly to the existing read/write/admin permission tiers. The method->
  permission mapping (GET/HEAD=read; projects/agents mutations=admin; other mutations=write) is
  unchanged — only the *source* of the grant moved from `scope` to `cognito:groups`.
- **Fails closed:** a token with no known group (or no `cognito:groups` claim) has an empty
  permission set -> 403 on anything needing read+. No wildcard/implicit grant.
- **Group names + claim are config-driven** (`AUTH_GROUP_ADMIN/WRITE/READ`, `AUTH_GROUPS_CLAIM`)
  so infra can rename groups without an app code change.
- **`COGNITO_AUDIENCE` widened to the agents + UI app-client ids.** Cognito access tokens carry no
  `aud`, so `client_id` is matched; both clients now issue access tokens gated by `cognito:groups`.
- **Agent token helper uses `USER_PASSWORD_AUTH` + `REFRESH_TOKEN_AUTH`.** `REFRESH_TOKEN_AUTH`
  returns a fresh AccessToken but no new RefreshToken, so the helper retains the existing refresh
  token and falls back to a full re-auth when it's missing/rejected. The boto3 cognito-idp client
  is created UNSIGNED because `InitiateAuth` is an unauthenticated API (no AWS creds needed); the
  Secrets Manager client stays signed (needs `secretsmanager:GetSecretValue`). Password/tokens are
  never logged, put in exception text, or shown by repr/__main__.

## HA-2 — Invite system design decisions
- **Invites live in a DEDICATED table (`${name_prefix}-invites`), not the app single-table store.**
  Invites are an auth artifact, not a storage-abstraction entity, so the admin endpoint reaches
  them via boto3 directly (not `current_app.storage`). Key = `code_hash`.
- **Store the HASH of the code, never the plaintext.** The code is 128-bit `secrets.token_urlsafe`
  entropy, so a plain SHA-256 (no pepper) is sufficient: a table dump cannot be reversed to a live
  code. Same for the optional email-binding (SHA-256 of the normalized address).
- **Email-binding enforced INSIDE the conditional burn**, as
  `(attribute_not_exists(email_binding) OR email_binding = :eb)`, rather than bird's pre-read +
  backstop. This makes the burn a single atomic UpdateItem with NO get-then-update TOCTOU, and the
  PreSignUp role needs only `dynamodb:UpdateItem` (no GetItem). A wrong-email attempt fails
  identically to a missing/used/expired code (no oracle).
- **Approval is by GROUP, not a status attribute (shared HA-3 contract).** PreSignUp adds the new
  user to NO group => pending; the app 403s them until an admin grants spec-readers. The `approved`
  marker is stored on the invite for a FUTURE PostConfirmation hook (this trigger deliberately
  calls no Cognito admin API, keeping its role UpdateItem-only). No `custom:status` attribute.
- **cognito.tf is NOT edited by HA-2.** invites.tf receives the pool ARN via
  `var.cognito_user_pool_arn` (for the `aws_lambda_permission` invoke grant, `count`-gated so
  validate passes pre-cutover) and OUTPUTs `presignup_lambda_arn`; the HA-3 pool cutover wires the
  pool's `pre_sign_up` trigger and passes the ARN back.
- **App-Lambda invites access attached in invites.tf** (a scoped `aws_iam_role_policy` on the
  iam.tf `lambda_exec` role, referenced read-only) rather than editing iam.tf, so the two files
  stay merge-conflict-free — same pattern reaper.tf uses to reference sibling resources.
- **`require_api_key` gained an optional `required` permission override** so the admin endpoints
  can hard-pin BOTH their GET and POST to `admin` (a plain GET would otherwise only need `read`).
  Additive + backward-compatible: default `None` keeps the method+blueprint derivation for every
  existing caller.

## DEC — 2026-07-21 — HA-5 admin user lifecycle: group-based approval + fail-closed self-guard
- **Approval is by Cognito GROUP, not a stored status column.** A pending human sits in NO spec-*
  group; approve adds spec-readers/spec-writers, promote adds spec-admins, reject/block disables the
  Cognito user AND strips its spec-* groups. Derived `status` (pending/active) is computed from group
  membership at list time. This keeps Cognito the single source of truth for who may sign in and with
  what permission, reusing the existing group->permission model (AUTH-10) with no new storage.
- **Added `cognito-idp:ListUsersInGroup` to the app Lambda IAM beyond the task's enumerated action
  list.** The last-admin demote guardrail must enumerate the spec-admins group; there is no way to
  count admins with only the enumerated actions. It stays least-privilege (scoped to the one pool ARN).
- **Self-protected mutations (block/reject/delete/demote) fail closed (501) under static API_KEYS
  auth.** The self-lockout guard reads the caller identity from the VERIFIED JWT (g.cognito_claims via
  helpers.current_identity()); static-key auth carries no per-caller identity, so rather than run the
  guard blind these mutations refuse. The canonical deploy always sets COGNITO_ISSUER, so this only
  affects a misconfiguration. Reviewer + security both flagged the gap independently.
- **Tests follow the HA-2 fake-client monkeypatch pattern (no moto).** moto is not a project
  dependency; an in-memory FakeCognito monkeypatched into admin._cognito_client mirrors the existing
  FakeTable approach for invites, keeping the test suite dependency-free.

## DEC — 2026-07-22 — HA-7 signup queue: bounded Path A (synchronous provisioning; optional pepper; WORM deferred)
- **Provisioning is SYNCHRONOUS on approve, not a second SQS queue.** The task allowed "provisioner
  Lambda OR synchronous on approve". The admin approve endpoint (app Lambda) mints the HA-2 invite +
  SES-es the join link + stamps `provisioned` inline. This drops a whole SQS provisioning queue +
  Lambda while keeping the load-bearing decoupling — the INTAKE queue — which is what makes the
  public path existence-free. Ordering: mint+email BEFORE the terminal `mark_provisioned` stamp, so a
  failure leaves the row `admin-approved` for a safe retry (at worst a second harmless TTL-invite to
  the same owner); mark_provisioned is conditional (`attribute_not_exists(provisioned_at)`) so a
  concurrent double-approve can only stamp once.
- **email_hash uses an OPTIONAL pepper (SIGNUP_PEPPER), plain SHA-256 fallback.** The bird design
  peppers email_hash via a Secrets-Manager secret. Bounded: a terraform var (sensitive, default "")
  wired identically to the app + worker Lambdas; unset → plain SHA-256 (fine for dev). Avoids standing
  up Secrets Manager + its IAM for this internal admin surface while keeping the faithful HMAC path
  available in prod. The plaintext email is stored ONLY as an SSE-KMS attribute value, never a key/GSI.
- **S3 WORM audit bucket + peppered ip/ua fingerprints DEFERRED (documented, not built).** Per the
  task's explicit defer list. The enumeration-privacy crux is preserved without them: uniform-202,
  the async existence branch behind SQS, hashed-identity-only logs, single-use hash-only token,
  constant-time verify.
- **Existing-user "already have an account" notice is CAPPED (signup.bump_notify).** Security review
  flagged that branch (a) of the worker had no per-email cap (the pending path has bump_resend, but a
  registered user has no profile row to count against) → a replayed known-registered victim address
  is a mail-bomb amplifier. Fixed with a standalone TTL'd `NOTIFY#<eh>` conditional counter mirroring
  bump_resend; still async/owner-only, so never an enumeration oracle.
- **Worker Lambda vendors a byte-identical copy of app/signup.py** (mirrors the bird "common/ copied
  into each lambda zip" packaging) so `terraform validate`/the archive-file zip need no build tooling;
  a re-vendor step + a diff assertion keep the two copies in lockstep.

## ONBOARD-3 — public agent-enrollment redeem (burn/provision ordering)
- **Burn FIRST (atomic, one winner), THEN provision.** Strict single-use is the top
  priority for this public, unauthenticated, credential-minting route. The redeem endpoint
  consumes the token with ONE conditional `UpdateItem` (`status active->used AND expires_at > now`)
  BEFORE any Cognito call — so two callers racing the same token can never both provision (the
  loser gets the same generic 400 as a missing/expired/used token; no enumeration oracle). Mirrors
  the PreSignUp trigger's `_burn`.
- **Provision failure after a successful burn → 500; the token stays spent (we NEVER un-burn).**
  Un-burning to "recover" would reopen the double-spend window this route exists to close. The
  documented remedy is to mint a FRESH enrollment token (tokens are cheap, single-use, TTL-bounded).
  The 500 body is generic; the token/password are never logged.
- **Provisioning is idempotent so a re-mint for the same `agent_name` still yields working creds.**
  AdminCreateUser tolerates `UsernameExistsException` (resolve the existing `sub` via AdminGetUser),
  and AdminSetUserPassword(permanent) + AdminAddUserToGroup run on both the fresh and existing user.
  add_member is an idempotent upsert. So the "token spent, provision failed → re-mint" recovery
  always converges on a usable credential without ever weakening single-use.
- **Capability tier is `spec-writers` ONLY; project membership is the enrolled role on the ONE
  enrolled project.** Never spec-admins, never multiple projects — least privilege for a self-served
  agent.

## ONBOARD-3a — close the cross-tenant agent-identity collision (P1)
- **The provisioned Cognito username is PROJECT-NAMESPACED, not derived from `agent_name` alone.**
  ONBOARD-3 keyed the redeem-flow Cognito user off `{agent_name}@{ENROLL_AGENT_DOMAIN}`, so the SAME
  `agent_name` in two DIFFERENT projects mapped to ONE shared user: redeeming the second reset that
  user's password and added it to the second project — a cross-tenant credential/membership
  escalation. The username is now
  `{sanitize(agent_name)}.{sanitize(project_slug)}.{h}@{ENROLL_AGENT_DOMAIN}` where `sanitize`
  lowercases + restricts to `[a-z0-9._-]` (each piece bounded to 20 chars) and `h` is the first
  16 hex (64 bits) of `SHA-256(agent_name "\0" project_slug)` — 20+20+16+2 = 58 ≤ the 64-char email
  local-part cap. The SAME `(project, agent_name)` is deterministic — so a re-enroll is a legitimate
  password ROTATION of the SAME user, matching ONBOARD-3's "member of exactly one project" intent —
  while two DISTINCT pairs collide only if sanitization aliases their visible local-parts AND their
  64-bit digests birthday-coincide: astronomically remote and never attacker-targetable (mint is
  project-admin gated, slugs are unique). NUL joins the pair (it can appear in neither component) so
  the boundary is unambiguous (chosen 64-bit over an initial 32-bit tag after security/reliability
  review flagged 32 bits as birthday-thin for a credential-isolation boundary). The `email` attribute stays equal
  to this username; the group (`spec-writers`) and the membership role are unchanged. The 19 platform
  agents provisioned by `scripts/enrol_agents.py` (`{name}@agents.spec-server.internal`, not via
  redeem) are untouched — only the redeem-flow derivation changed.
- **Mint refuses a second ACTIVE enrollment for the same `(project_slug, agent_name)` (generic 409).**
  Two concurrent live tokens for one target would let two redeems race to provision/rotate the same
  user. `POST /admin/agent-enrollments` now scans the enrollments table and rejects with a generic
  409 if an active, unexpired row for that pair exists (used/expired/revoked rows do not count, so a
  fresh mint is allowed once the prior token is spent or lapses). The pair is compared in-process
  (mirrors the list endpoint's read); no caller value is formatted into a DynamoDB expression. This
  409 is a BEST-EFFORT sequential guard (a scan-then-put has a TOCTOU window under concurrent mints),
  NOT the isolation invariant: the invariant is the deterministic project-namespaced username +
  idempotent provisioning, so even two coexisting active tokens for one pair redeem to the SAME
  Cognito user (a rotation) and never cross tenants. A future hardening could make it a hard
  invariant via a conditional write on a deterministic per-pair guard key.

## DEC-PORT-8: full-fidelity JSON migration transport is idempotent on `public_id`, targets a fresh store

**Context.** The `SPEC.md` text round-trip anchors and dedups tasks on their human `key`
(`- [ ] EPIC-N · title`). A task with **no key has no representation**, so keyless tasks silently
drop on a text export→import (a real project lost 267 keyless follow-up tasks). We need a lossless
transport for migrating a whole project.

**Decision.** Add an **additive** JSON format alongside the text one (never changing the text
behaviour): `GET .../export?format=json` (or `Accept: application/json`) emits **every** task —
keyed AND keyless — plus epics/tags/timestamps; `POST .../import` with `Content-Type:
application/json` upserts each task **idempotently on its stable `public_id`** (not its key), so
keyless tasks dedup by their id and round-trip losslessly. Both storage backends implement it
identically (same dedup key, counts, unchanged-detection field set, tag/epic handling, batched
writes). Runtime state (`owner`, `lease_expires_at`, `version`) is **excluded** — a fresh import
starts each task unowned at `version` 1. Epics dedup on their `key`; their `public_id` is minted
fresh on import (it is not an idempotency anchor and preserving it would collide on Postgres'
global-unique `public_id`). Within a single payload, tasks are de-duplicated by `public_id`
(last-wins) on both adapters for parity.

**Consequences.** `import(export(project))` reproduces all tasks with fields+tags+epic and preserved
task `public_id`; re-import into the same project is a genuine no-op (0 writes); a changed field
re-imports as one update. Because a task's `public_id` is **globally unique on Postgres** (per-
partition on DynamoDB), the transport targets a **fresh** project/server — importing a payload whose
`public_id` already exists in a *different* project of the same Postgres store would raise (Dynamo
would create in its own partition). This is acceptable for the migration use case; a future
hardening could make `public_id` per-project unique to close the last cross-backend edge.
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

## DEC-9 — 2026-07-14 — Add GET for task relations and chain-runs (were write-only)

**Context.** `POST /tasks/{ident}/relations` and `POST /tasks/{ident}/chain-runs` existed to
*create* edges/runs, but there was no way to read them back short of a single chain-run's own
`GET /chain-runs/{run_pubid}` (which requires already knowing its `public_id`). A full-project
data extract (em-tracker's daily backup) surfaced this: relations created earlier in the project's
life were completely invisible via the API, and there was no way to enumerate a task's chain-run
history at all.

**Decision.** Add `GET /tasks/{ident}/relations` (both directions, tagged `outgoing`/`incoming`
from the requested task's perspective, each entry naming the *other* task) and
`GET /tasks/{ident}/chain-runs` (oldest-first, each with its steps, reusing the existing
`ChainRunOut` schema). Added `Task.outgoing_relations`/`incoming_relations`/`chain_runs`
relationships and `TaskRelation.src_task`/`dst_task` back-populates to `models.py` — no schema
migration needed (ORM-only, no new columns/tables).

**Consequences.** Relation and chain-run data is now actually recoverable via the API instead of
being write-only. No new project-wide "list all relations" endpoint was added — only per-task,
matching the existing per-task shape of notes/commits. If a project-wide relations view is needed
later, add it then rather than speculatively now.

## DEC-10 — 2026-07-24 — Change-log cursor via the atomic counter (UI-DELTA)

**Context.** The dashboard refetches the whole backlog every poll (see
`UI_DATA_LOADING_DEEPDIVE.md`). To serve *deltas* the server needs a monotonic, total-ordered,
per-project cursor that behaves IDENTICALLY on both storage backends. The existing event log is
unfit: no exposed cursor, most task mutations emit nothing, no deletion tombstones, and its
DynamoDB `ts#uuid` tiebreak differs in shape from the Postgres serial id (deep-dive §2/§4).

**Decision.** Introduce a per-project **change-log**. Each entry is
`{seq, entity_type, entity_pubid, op ∈ {upsert,delete}, version, occurred_at, snapshot}` where:

- **`seq` is allocated by the already-proven atomic counter** (`reserve_number` primitive) under a
  per-project namespace `changelog` — Postgres `INSERT … ON CONFLICT DO UPDATE … RETURNING`,
  DynamoDB per-item `ADD current_value :1`. Never read-max-plus-one. The cursor is a plain
  per-project integer with identical semantics on both backends → parity by construction.
- **Storage.** Postgres: a `changes` table, `UNIQUE(project_id, seq)` + index `(project_id, seq)`
  (migration `g1changes`). DynamoDB: a change item `PK=P#<slug>`, `SK=CHANGE#<seq %020d>` (zero-
  padded so lexical order == numeric order) plus **GSI7** (`GSI7PK=P#<slug>#CHANGES`,
  `GSI7SK=<seq %020d>`, projection ALL) for the ascending `seq > cursor` range query. GSI7 is the
  reserved index number 7 (namespace `dynamo-gsi`), mirroring the GSI1-6 pattern.
- **Pointer = `public_id`** (stable, cross-backend), standardising on UI-DELTA-1's event fix.
- **Snapshot.** `op=upsert` embeds the entity's current DTO; for tasks it is a **lean** snapshot
  (§6.9) — the scalar `TaskOut` fields + `tags`, OMITTING the nested `notes[]`/`commits[]` to bound
  feed size. `op=delete` is a **tombstone** carrying only `entity_type + entity_pubid` (snapshot
  and version null) so a client can evict.
- **Atomicity (hard requirement).** The change entry is written in the SAME transaction (Postgres)
  / `TransactWriteItems` (DynamoDB) as the entity mutation, so the two are all-or-nothing. A change
  written after a separate commit could be lost on failure → a silent feed gap the client never
  recovers from; that is unacceptable. On DynamoDB the previously single-`PutItem` mutations
  (create/update/set_status/release/add_commit/create_epic/update_epic/notes) and the conditional
  claim `UpdateItem` are folded into a `TransactWriteItems` with the change Put; `complete`/
  `supersedes` (already transactional) simply gain the extra Put.
- **Coverage.** Fills every §2.2 gap — `create_task`, `update_task`, `set_status`, `release_task`,
  `delete_task` (tombstone), `add_commit`, `add_relation` (records the source task), `create_epic`,
  `update_epic` — AND the mutations that already emit events (`claim`, `complete`, task/epic
  `note`). Commits, relations and notes ride an **upsert of their parent task/epic** (the entity the
  UI cache is keyed by) rather than a distinct entry, since notes/commits/relations have no stable
  cross-backend `public_id`; a duplicate commit is a genuine no-op and records nothing (so the
  sequential `seq` stays gap-free).

**Consequences.** The cursor is monotonic, total-ordered and gap-free under sequential mutation on
both backends (concurrent conditional-write losers may skip a `seq` — a harmless contiguity gap,
never a duplicate, exactly as the reservation allocator already accepts). The write path is
**additive and inert**: nothing reads the change-log yet, so existing task/epic/event responses are
unchanged (the only new observable is a `changelog` counter appearing in `list_counters`). The
delta/head HTTP endpoints, retained-window/`full_resync_required` semantics and the client cache are
UI-DELTA-5+; this task lands only the write path plus a `changes_head(slug)` cursor read and a
`list_changes(slug, since, limit)` storage method (GSI7 on DynamoDB) used to verify it.

## UI-DELTA-10: batched head fan-out as a dedicated `/projects/heads` endpoint (not a `/projects` field)

**Context.** A dashboard showing many projects would poll `/changes/head` once per project each tick
(an N-request fan-out). The deep-dive (§5.1 item 4) suggested "optionally fold a per-project head map
into `GET /projects`".

**Decision.** Ship the head map as a SEPARATE batch endpoint `GET /api/v1/projects/heads` →
`{"heads": {<slug>: {cursor, min_retained_seq}}}` rather than adding a `head_cursor` field to
`ProjectOut`. It is isolation-scoped to the caller's visible projects using the exact same
`_visible_projects()` filter as `GET /projects`, and takes NO caller-supplied slug list (the input is
bounded by the visible set, so the fan-out can never be widened and a non-member slug can't be
probed). It goes through a new `changes_heads_for(slugs)` storage port on BOTH adapters (Postgres
grouped `max/min(seq)` read; DynamoDB reuses `changes_head` + base reads — no new GSI).

**Why not fold into `/projects`.** Adding the head to every `ProjectOut` (a) changes a widely-consumed
contract and (b) makes every `/projects` caller pay the per-project head cost even when it doesn't
need cursors. A dedicated endpoint keeps `/projects` unchanged, makes the head poll opt-in for the
fan-out pages, and isolates the extra read cost to exactly the callers that want it — lower-risk, same
one-request win.

**Consequence / caveat.** A project literally slugged `heads` is shadowed by the static route on
`GET /projects/heads` (Werkzeug ranks static > dynamic). This is isolation-safe (the batch is
membership-scoped) and low-risk; if `heads` ever needs to be a real slug, reserve it or gate the batch
behind a query flag.

## 2026-07-24 — INFRA: keep the INFRA-7 rationale header when regenerating requirements.lock

**Decision.** When regenerating `requirements.lock` with `pip-compile 7.6.0`, restore the
hand-written INFRA-7 documentation header (the two-line `-P greenlet==3.2.5 -P psycopg==3.2.13`
regeneration command and the prose explaining *why* greenlet/psycopg are pinned below latest) on
top of the freshly-generated resolved section.

**Why.** pip-compile 7.6.0 records only the persistent invocation in the header and strips the
transient `--upgrade-package` (`-P`) flags, and it never emits explanatory prose. Left as-is, the
regenerated lock would silently lose the record of *which* pins are load-bearing and *why* — so the
next person regenerating (without the `-P` overrides) would resolve greenlet/psycopg to versions
that ship no `manylinux2014_aarch64` wheel and break `scripts/build_lambda.sh`. The header is a
comment: it does not affect pip's parsing or the `--generate-hashes` integrity (verified: the
platform `pip download` resolves with `PIP_EXIT=0`), and the resolved package section is left
byte-for-byte as pip-compile emitted it — so this stays a genuine pip-compile artifact while
preserving the INFRA-7 pin rationale the task explicitly asked to keep.

## 2026-07-24 — SLS-J1: cross-backend Jira TaskDTO + narrowing the jira test-deferral

**Decision.** Add `jira_issue_key` / `jira_sync_error` to the backend-neutral `TaskDTO` and have
BOTH adapters populate them (Postgres reads the ORM columns; DynamoDB reads the item attributes and
`create_task` seeds both to `None`). In `tests/conftest.py`, remove the blanket
`TestJiraFieldsInResponse` skip and let the two HTTP-level response-schema methods run cross-backend,
while `test_get_task_with_jira_values_set` (which seeds values via `db.session`) stays Postgres-only.

**Why the conftest change is more than a pure branch removal.** The task specified "just remove the
`TestJiraFieldsInResponse` deferral branch", but the surviving `if "test_jira_" in nid` fall-through
matches on the *nodeid*, and every test in `tests/test_jira_schema_fields.py` has `test_jira_` in its
path — so a pure removal leaves the two response methods marked `postgres_only` and SKIPPED on
DynamoDB (empirically confirmed: 7 passed / 7 skipped). That directly contradicts the task's own
proof ("`TestJiraFieldsInResponse` green on BOTH backends"). The minimal fix that satisfies the
proof is an explicit `continue` for the two response-schema method names *before* the fall-through,
so they run cross-backend while the ORM-seeding test still falls through to `postgres_only`.

**Consequence.** `TestJiraFieldsDumpOnly` and `TestJiraFieldsInOpenAPI` remain Postgres-only (schema/
OpenAPI-level, backend-neutral, unchanged from before) — left untouched to keep the change minimal
and scoped to exactly the two methods the task named.

## 2026-07-24 — SLS-J2: relations-GET via a DynamoDB relation-MIRROR item (D1)

**D1 (given).** `list_relations` returns a task's incoming edges on DynamoDB via a **mirror item**,
not a new GSI. `add_relation` writes, in the SAME `TransactWriteItems` as the forward edge
`SK = TASK#<src>#REL#<kind>#<dst>`, a second item `SK = TASK#<dst>#RELIN#<kind>#<src>` under the
destination task's child collection. `list_relations` is then two `begins_with` range reads on the
task's own partition — `TASK#<ident>#REL#` (outgoing) and `TASK#<ident>#RELIN#` (incoming) — so
incoming edges need no index. The pair is written atomically, so an edge and its mirror never
diverge. Chosen precisely to avoid any new GSI / `infra/terraform` change / redeploy. Verified the
`RELIN` prefix does not alias the `REL#` prefix (the char after `REL` is `I`, not `#`), and that
`_load_task_full` still ignores mirror items (it only collects `#COMMIT#`/`#NOTE#` children), so task
loads are undisturbed (`test_supersede_relation[dynamodb]` green).

**Backfill follow-up (do NOT run here).** Pre-existing production relations were written BEFORE the
mirror item existed, so they have a forward `REL#` item but no `RELIN#` mirror. Until a one-shot
backfill writes a `RELIN` mirror for every existing forward relation, `GET .../relations` will return
outgoing edges for old data but MISS incoming edges for those pre-mirror relations. A backfill task
(scan `type=relation` items, write the matching `relation_in_sk` mirror idempotently) is required
before relations-GET is trustworthy for incoming edges on historical data. Tracked as a follow-up;
not executed in SLS-J2.

**Parity fix (Postgres).** Un-deferring the relations-GET tests surfaced a latent Postgres bug:
`PostgresBackend._task` cast a non-UUID ident (e.g. a bogus human key `NOPE-1`) directly against the
UUID-typed `public_id` column, raising `DataError` (HTTP 500) instead of `NotFound` (404). The
DynamoDB adapter already 404s cleanly. `_task` now guards the `public_id` lookup behind a
`uuid.UUID(ident)` parse, so an unknown ident 404s on both backends
(`test_get_relations_404_unknown_task` green on postgres + dynamodb). This restores backend parity
for every task-by-ident endpoint, not just relations-GET.
