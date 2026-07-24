# Storage Abstraction Deep-Dive — Switchable Postgres / DynamoDB Backend

**Task:** SLS-1 (design only — no app code changes).
**Author:** sls-designer.
**Date:** 2026-07-21.
**Scope of study:** `app/models.py`, `app/services.py`, `app/idempotency.py`, `app/helpers.py`,
`app/schemas.py`, `app/config.py`, `app/extensions.py`, `app/__init__.py`, every
`app/blueprints/*.py`, `CLAUDE.md` "Concurrency invariants", `AGENTS_API.md`,
`infra/terraform/main.tf`. Read in full (not sampled) unless noted.

The goal: make the storage layer switchable behind a repository/port interface —
`STORAGE_BACKEND=postgres|dynamodb`, default `postgres` locally — keeping Postgres as the
reference implementation and adding DynamoDB as a config-selected second backend, **without
regressing the two atomic guarantees or the optimistic-lock/412 contract.**

---

## 0. Executive summary

- The app's entire value rests on three Postgres-specific primitives:
  1. **Atomic claim** — `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` (`services.py:133-137`).
  2. **Atomic reservation** — `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`
     (`services.py:54-66`), backstopped by `UNIQUE(project_id, namespace, value)`
     (`models.py:363-367`).
  3. **Optimistic locking** — `version` column + `If-Match`/412 (`helpers.py:58-73`,
     increments at `tasks.py:208,242,269,291`).
- All three map cleanly onto DynamoDB conditional writes; **only** the multi-item
  transactional cases (claim writes task+lease+event; complete writes task+commit+lease+event;
  supersedes writes relation+dst-task) need `TransactWriteItems` to keep Postgres-equivalent
  atomicity. Everything else is a single-item conditional op.
- Recommended shape: a **backend-neutral repository interface** (`StorageBackend`) returning
  plain DTOs (not ORM objects) + backend-neutral errors (`NotFound`, `VersionConflict`,
  `Conflict`), with blueprints calling `current_app.storage.<method>()` instead of
  `db.session`. A **single-table DynamoDB design** (one table, ~5 GSIs) serves every access
  pattern below with **no Scan**.
- Biggest refactor landmine: blueprints/schemas today dump **ORM objects with lazy
  relationships** (`task.epic.key`, `task.tags`, `task.commits`, `task.notes` —
  `schemas.py:159-171`, `TaskOut`). The abstraction must return fully-materialised DTOs; this
  is a prerequisite task, not an afterthought (recommend splitting it out — see §6).

---

## 1. Current data model + the access-pattern contract

### 1.1 Entities (from `app/models.py` + `app/idempotency.py`)

| Entity | Table | Identity / uniqueness | Notes |
|---|---|---|---|
| Project | `projects` | `slug` unique, `public_id` uuid | API addresses projects by **slug** |
| Agent | `agents` | `UNIQUE(project_id, slug)` | per-project roster |
| Epic | `epics` | `UNIQUE(project_id, key)` | `position` float ordering |
| Task | `tasks` | `UNIQUE(project_id, key) WHERE key NOT NULL`; `public_id` uuid | see indexes below |
| Tag / TaskTag | `tags`, `task_tags` | `UNIQUE(project_id, key)` | many-to-many |
| TaskRelation | `task_relations` | `UNIQUE(src,dst,kind)`, `src<>dst` | blocks/supersedes/relates/follow_up |
| CommitRef | `commit_refs` | `UNIQUE(task_id, sha)` | |
| TaskNote / EpicNote | `task_notes`, `epic_notes` | append-only, ordered by `created_at` | |
| Counter | `counters` | **composite PK `(project_id, namespace)`** | the reservation allocator |
| Reservation | `reservations` | `UNIQUE(project_id, namespace, value)` | audit trail + backstop |
| Lease | `leases` | **partial unique `WHERE state='active'`** (`models.py:386-395`) | one active lease/task |
| Event | `events` | append-only, `ix(project_id, created_at)` | replaces AGENT_LOG.md |
| Decision | `decisions` | `public_id`, optional `key` | replaces DECISIONS.md |
| ChainRun / ChainStep | `chain_runs`, `chain_steps` | `UNIQUE(run_id, step_name)` | mandated-chain audit |
| IdempotencyKey | `idempotency_keys` | `UNIQUE(project_id, endpoint, key)` | replay store |

Task indexes that the DynamoDB model must reproduce as GSIs:
- `ix_tasks_claim (project_id, status, priority, position)` — `models.py:173-179` → **claim / status-list GSI**.
- `ix_tasks_owner (project_id, owner)` — `models.py:180` → **owner GSI ("my specs")**.
- `uq_task_project_key` — task lookup by human `key` (URLs accept key OR public_id,
  `helpers.py:42-55`) → **key GSI**.

### 1.2 Access patterns (the contract both adapters MUST satisfy)

Derived by reading every blueprint. This is the exhaustive list; each row is a method on the
interface (§2).

**Projects** (`projects.py`)
1. `create_project`
2. `get_project(slug)` — by slug (`helpers.py:24-30`)
3. `list_projects` — all, ordered by slug
4. `update_project(slug, patch)`
5. `delete_project(slug)` — **cascade to all children**

**Agents** (`agents.py`)
6. `list_agents(project)` — ordered by slug
7. `upsert_agent(project, slug, ...)` — insert-or-update by `(project, slug)`

**Epics** (`epics.py`)
8. `list_epics(project)` — ordered by (position, key)
9. `create_epic` — 409 on duplicate key
10. `get_epic(project, key)`
11. `update_epic(project, key, patch)`
12. `list_epic_notes(project, key)` — oldest first
13. `append_epic_note(project, key, note)`

**Tasks** (`tasks.py`) — the concurrency-critical surface
14. `list_tasks(project, {status, owner, priority, epic, tag, q, limit, offset})` — ordered by (position, id)
15. `create_task` — 409 on duplicate key; attach tags; resolve epic_key
16. `get_task(project, ident)` — by key then public_id; returns ETag (`version`)
17. `update_task(project, ident, patch, expected_version)` — **If-Match → 412**; bumps version
18. `delete_task(project, ident)`
19. **`claim_next(project, agent, {epic, priority_max, component, lease_ttl})`** — atomic
20. **`complete_task(project, ident, {commit_sha, repo, test_summary, proof_cmd}, expected_version)`** — flip done + close lease + write CommitRef + event
21. `release_task(project, ident, reset_to)` — clear owner/lease, close lease
22. `set_status(project, ident, status, note, expected_version)` — If-Match
23. `add_commit(project, ident, commit)` — dedupe by (task, sha)
24. `list_task_notes(project, ident)` / `append_task_note(project, ident, note)`
25. `add_relation(project, ident, target, kind)` — supersedes flips dst status + `superseded_by`

**Reservations / counters** (`reservations.py`)
26. **`reserve_number(project, namespace, reserved_by, task_key, note)`** — atomic + audit
27. `list_reservations(project, namespace?)` — ordered (namespace, value)
28. `list_counters(project)` — current values per namespace

**Events / notes / decisions** (`log.py`)
29. `log_event(project, type, agent, task, message, payload)` — auto-emitted on claim/complete/reserve/note/decision
30. `list_events(project, {event_type, agent, task, limit, offset})` — newest first
31. `list_project_notes(project, {scope, author, task, epic, since, limit, offset})` — merged task+epic feed, newest first
32. `list_decisions(project)` / `create_decision(project, ...)`

**Chains** (`chains.py`)
33. `create_chain_run(project, ident, started_by)`
34. `get_chain_run(project, run_pubid)` — with steps
35. `update_chain_run(project, run_pubid, status)`
36. `upsert_chain_step(project, run_pubid, step_name, ...)` — by (run, step_name)

**Ports** (`ports.py`)
37. `import_spec(project, parsed)` — idempotent bulk upsert (`services.py:164-219`)
38. `export/render(project)` — reads all epics + tasks (can stay in service layer over
    `list_epics`/`list_tasks`)

**Idempotency** (`idempotency.py`)
39. `lookup_idempotent(project, endpoint, key)`
40. `store_idempotent(project, endpoint, key, response_json, status_code)` — race-safe insert

---

## 2. Proposed repository / port interface

### 2.1 File layout

```
app/storage/
    __init__.py     # make_storage(config) factory + StorageBackend re-export
    base.py         # StorageBackend ABC/Protocol (the port) + DTOs
    errors.py       # NotFound, VersionConflict, Conflict, BackendUnavailable
    dto.py          # frozen dataclasses: ProjectDTO, TaskDTO, EpicDTO, NoteDTO, ...
    postgres.py     # PostgresBackend — wraps today's SQLAlchemy code (reference impl)
    dynamo.py       # DynamoBackend — boto3 single-table
    keys.py         # DynamoDB key builders (PK/SK/GSI encoders) — dynamo-only helper
```

`create_app()` builds the backend once and stashes it (`app.storage = make_storage(app.config)`);
blueprints call `current_app.storage.<method>()`. Blueprints stop importing `db.session`,
`models`, and `services` directly. `app/services.py` is absorbed into `postgres.py`
(the two atomic ops become methods); `app/idempotency.py`'s ORM model moves to `postgres.py`,
its interface (`lookup`/`store`) becomes two backend methods.

### 2.2 DTOs, not ORM objects (load-bearing)

Today `TaskOut` dumps a live ORM `Task` and walks relationships: `obj.epic.key`,
`[t.key for t in obj.tags]`, `commits`, `notes` (`schemas.py:159-171,166-170`). Marshmallow
`Nested`/`Method` fields must instead dump plain objects. Define frozen dataclasses whose
attribute names match the schema field sources (`epic_key`, `tags: list[str]`, `commits`,
`notes`, `display_id`, `version`, ...). Both backends return these. This decouples the HTTP
layer from SQLAlchemy entirely and is what makes the switch possible.

### 2.3 Backend-neutral errors

Blueprints today rely on `flask_smorest.abort` + SQLAlchemy `IntegrityError`. Replace with:

| Error | Raised when | HTTP mapping (registered error handler) |
|---|---|---|
| `NotFound` | slug/key/public_id absent | 404 |
| `Conflict` | duplicate key / duplicate reservation value | 409 |
| `VersionConflict` | optimistic-lock mismatch | **412** |
| `BackendUnavailable` | Dynamo throttle after retries / PG down | 503 |

The blueprint catches nothing new; a single `@app.errorhandler` per type keeps parity with
today's `abort(...)` codes and the existing `check_if_match` 412 (`helpers.py:66-72`).

### 2.4 Interface sketch (key signatures)

```python
# app/storage/base.py
class StorageBackend(Protocol):

    # --- health ---
    def ping(self) -> None: ...   # raises BackendUnavailable; cheap liveness probe for /readyz

    # --- projects / agents / epics ---
    def get_project(self, slug: str) -> ProjectDTO: ...            # raises NotFound
    def list_projects(self) -> list[ProjectDTO]: ...
    def create_project(self, data: dict) -> ProjectDTO: ...        # raises Conflict
    def delete_project(self, slug: str) -> None: ...               # cascade
    def upsert_agent(self, slug: str, data: dict) -> AgentDTO: ...
    def list_epics(self, slug: str) -> list[EpicDTO]: ...
    def create_epic(self, slug: str, data: dict) -> EpicDTO: ...   # raises Conflict

    # --- tasks: CRUD + optimistic lock ---
    def get_task(self, slug: str, ident: str) -> TaskDTO: ...      # by key|public_id
    def list_tasks(self, slug: str, flt: TaskFilter) -> list[TaskDTO]: ...
    def create_task(self, slug: str, data: dict) -> TaskDTO: ...
    def update_task(self, slug: str, ident: str, patch: dict,
                    expected_version: int | None) -> TaskDTO: ...  # raises VersionConflict
    def delete_task(self, slug: str, ident: str) -> None: ...

    # --- the two atomic guarantees ---
    def claim_next(self, slug: str, agent: str, *, epic: str | None = None,
                   priority_max: str | None = None, component: str | None = None,
                   lease_ttl: int | None = None) -> TaskDTO | None: ...   # None => 204
    def complete_task(self, slug: str, ident: str, data: dict,
                      expected_version: int | None) -> TaskDTO: ...
    def release_task(self, slug: str, ident: str, reset_to: str) -> TaskDTO: ...
    def set_status(self, slug: str, ident: str, status: str, note: str | None,
                   expected_version: int | None) -> TaskDTO: ...
    def reserve_number(self, slug: str, namespace: str, *, reserved_by=None,
                       task_key=None, note=None) -> ReservationDTO: ...
    def list_reservations(self, slug: str, namespace: str | None) -> list[ReservationDTO]: ...
    def list_counters(self, slug: str) -> list[CounterDTO]: ...

    # --- children / feeds ---
    def add_commit(self, slug, ident, commit) -> TaskDTO: ...
    def append_task_note(self, slug, ident, note) -> NoteDTO: ...
    def list_task_notes(self, slug, ident) -> list[NoteDTO]: ...
    def add_relation(self, slug, ident, target, kind) -> str: ...   # supersedes side-effect
    def log_event(self, slug, event_type, **kw) -> EventDTO: ...
    def list_events(self, slug, flt) -> list[EventDTO]: ...
    def list_project_notes(self, slug, flt) -> list[ProjectNoteDTO]: ...
    def create_decision(self, slug, data) -> DecisionDTO: ...
    def list_decisions(self, slug) -> list[DecisionDTO]: ...

    # --- chains ---
    def create_chain_run(self, slug, ident, started_by) -> ChainRunDTO: ...
    def get_chain_run(self, slug, run_pubid) -> ChainRunDTO: ...
    def update_chain_run(self, slug, run_pubid, status) -> ChainRunDTO: ...
    def upsert_chain_step(self, slug, run_pubid, step_name, data) -> ChainStepDTO: ...

    # --- ports / idempotency ---
    def import_spec(self, slug, parsed) -> dict: ...
    def lookup_idempotent(self, slug, endpoint, key) -> IdemDTO | None: ...
    def store_idempotent(self, slug, endpoint, key, resp, status) -> IdemDTO: ...
```

`PostgresBackend` implements each by lifting today's blueprint/service body verbatim (behaviour
parity — SLS-2). `DynamoBackend` implements each against the single table (§3).

---

## 3. DynamoDB single-table design

One table (Terraform: `infra/terraform/dynamodb.tf`, on-demand, PITR — already anticipated at
`infra/terraform/main.tf:12,33,47`). All of a project's data lives under partition
`P#<slug>`; the task item and its children share the `TASK#<public_id>` SK prefix so a single
`Query` returns the task plus its commits/notes/relations (the single-table "item + children"
pattern), mirroring today's ORM eager-load in `TaskOut`.

### 3.1 Key convention & item shapes

Base table: `PK` (S), `SK` (S).

| Entity | PK | SK |
|---|---|---|
| Project meta | `P#<slug>` | `META` |
| Agent | `P#<slug>` | `AGENT#<slug>` |
| Epic | `P#<slug>` | `EPIC#<key>` |
| EpicNote | `P#<slug>` | `EPIC#<key>#NOTE#<ts>#<uuid>` |
| **Task** | `P#<slug>` | `TASK#<public_id>` |
| TaskNote | `P#<slug>` | `TASK#<public_id>#NOTE#<ts>#<uuid>` |
| CommitRef | `P#<slug>` | `TASK#<public_id>#COMMIT#<sha>` |
| TaskRelation (forward) | `P#<slug>` | `TASK#<src_public_id>#REL#<kind>#<dst_pubid>` |
| TaskRelation (mirror, **SLS-J2/D1**) | `P#<slug>` | `TASK#<dst_public_id>#RELIN#<kind>#<src_pubid>` |
| Lease history | `P#<slug>` | `TASK#<public_id>#LEASE#<ts>` (TTL attr for GC only) |
| Tag adjacency | `P#<slug>` | `TAG#<key>#TASK#<public_id>` (for tag filter) |
| Counter | `P#<slug>` | `COUNTER#<namespace>` |
| Reservation | `P#<slug>` | `RES#<namespace>#<zero-padded value>` |
| Event | `P#<slug>` | `EVT#<ts>#<uuid>` |
| Decision | `P#<slug>` | `DEC#<ts>#<uuid>` |
| ChainRun | `P#<slug>` | `CRUN#<run_pubid>` |
| ChainStep | `P#<slug>` | `CRUN#<run_pubid>#STEP#<step_name>` |
| Jira config (singleton, **SLS-J3**) | `P#<slug>` | `JIRACFG` |
| Idempotency | `P#<slug>` | `IDEM#<endpoint>#<key>` |

`<ts>` = ISO-8601 UTC with millis (lexicographically sortable). `<zero-padded value>` keeps
`RES#` SKs in numeric order for `list_reservations`.

Example — a task item:

```json
{
  "PK": "P#spec-server",
  "SK": "TASK#3f2c...-uuid",
  "type": "task",
  "project_slug": "spec-server",
  "public_id": "3f2c...-uuid",
  "key": "SLS-3",
  "epic_key": "SLS",
  "title": "DynamoDB adapter: tasks + claim-next",
  "description": "...",
  "status": "todo",
  "priority": "P1",
  "priority_rank": 1,
  "component": "BE",
  "proof_cmd": null,
  "status_note": null,
  "section": "backlog",
  "owner": null,                       // ABSENT when unclaimed (sparse GSI2)
  "lease_expires_at": null,
  "position": 1300.0,
  "version": 1,
  "tags": ["storage","aws"],           // denormalised String Set
  "created_by": null,
  "created_at": "2026-07-21T10:00:00.000Z",
  "updated_at": "2026-07-21T10:00:00.000Z",
  "completed_at": null,

  "GSI1PK": "P#spec-server#ST#todo",   // claim/status index (present for tasks only)
  "GSI1SK": "1#0000001300.0#3f2c...",  // priority_rank # position # public_id
  "GSI3PK": "P#spec-server#KEY#SLS-3", // key lookup (present only when key != null)
  "GSI3SK": "TASK#3f2c..."
  // GSI2 (owner) attrs ABSENT while owner is null -> item not in owner index
}
```

Note the invariant: `owner` and `GSI2PK`/`GSI2SK` are **written together** and **removed
together**. `GSI1PK` embeds `status`, so the claim (todo→in_progress) automatically moves the
task between status partitions in GSI1 on write — no separate index maintenance.

### 3.2 GSIs (serve every pattern without a Scan)

| GSI | PK | SK | Serves | Sparse? |
|---|---|---|---|---|
| **GSI1** claim/status | `P#<slug>#ST#<status>` | `<priority_rank>#<pos>#<pubid>` | claim-next candidate query; `list_tasks?status=` ordered by priority/position | tasks only |
| **GSI2** owner | `P#<slug>#OWN#<owner>` | `TASK#<pubid>` | "my specs" `list_tasks?owner=` | only when `owner` set |
| **GSI3** task-key | `P#<slug>#KEY#<key>` | `TASK#<pubid>` | `get_task` by human key | only when `key` set |
| **GSI4** feed | `P#<slug>#FEED#<kind>` (`EVT`/`TN`/`EN`) | `<ts>#<uuid>` | events newest-first; project notes feed; per-task/epic note lists via begins_with on base table | events/notes |
| **GSI5** all-projects | `PROJECTS` | `<slug>` | `list_projects` | project-meta only |

Access-pattern → query map (all Query, never Scan):

- **list_tasks (no status filter):** `Query PK=P#<slug>, SK begins_with "TASK#"` +
  FilterExpression for priority/epic/q; order client-side by (position). For status-filtered
  or ordered lists use GSI1.
- **list_tasks?status=todo:** `Query GSI1 PK=P#<slug>#ST#todo` (already priority/position ordered).
- **list_tasks?owner=X:** `Query GSI2 PK=P#<slug>#OWN#X`.
- **list_tasks?tag=t:** `Query PK=P#<slug>, SK begins_with "TAG#t#TASK#"` → pubids → BatchGet.
- **get_task(key):** `Query GSI3 PK=P#<slug>#KEY#<key>` (1 item); **get_task(public_id):**
  `GetItem PK=P#<slug>, SK=TASK#<pubid>` then `Query begins_with TASK#<pubid>` for children.
- **list_reservations / list_counters:** `Query begins_with "RES#"/"COUNTER#"` on base table.
- **list_events / list_project_notes:** `Query GSI4` descending, `since` via SK range, filters
  as FilterExpression; merged task+epic notes = two GSI4 partitions merged client-side (mirrors
  the `log.py:70-127` merge-and-sort).
- **list_epics / list_agents / chain steps:** `Query begins_with` on base table.

### 3.3 The two atomic guarantees + optimistic lock on DynamoDB

**claim_next** (replaces `FOR UPDATE SKIP LOCKED`, `services.py:133-139`):

```
1. Query GSI1 PK = "P#<slug>#ST#todo"  (ScanIndexForward=True, Limit=25)
      + optional FilterExpression epic_key/component/priority_rank<=cutoff
2. For each candidate (in priority/position order):
      UpdateItem PK=P#<slug> SK=TASK#<pubid>
        ConditionExpression:
            attribute_not_exists(#owner) AND #status = :todo
        UpdateExpression:
            SET #status=:inprog, #owner=:agent, lease_expires_at=:exp,
                GSI1PK=:st_inprog, GSI2PK=:own, GSI2SK=:tk
            ADD version :one
      -> success: return the task (the winner)
      -> ConditionalCheckFailedException: someone else won -> try next candidate
3. Candidates exhausted -> re-query once; still none -> return None (=> 204)
```

Plus the **expired-lease reclaim** path (parity with `services.py:115-121`): a second candidate
query on `GSI1 PK="P#<slug>#ST#in_progress"` with `FilterExpression lease_expires_at < :now`,
claimed with condition `#status=:inprog AND lease_expires_at < :now`. Lazy reclaim is the
source of truth — **do not** rely on DynamoDB TTL for reclaim (TTL deletion lags up to 48h);
TTL only garbage-collects old `...#LEASE#<ts>` history items.

The GSI is eventually consistent, so a just-claimed task can briefly still appear as `todo` in
GSI1 — harmless: the **conditional write is the actual guard**, so a stale candidate just costs
one failed attempt, never a double-claim. This preserves invariant #1 exactly.

**reserve_number** (replaces `INSERT ... ON CONFLICT DO UPDATE RETURNING`, `services.py:54-66`):

```
TransactWriteItems([
  Update  PK=P#<slug> SK=COUNTER#<ns>
          UpdateExpression: "ADD current_value :one"
          ReturnValuesOnConditionCheckFailure omitted
  Put     PK=P#<slug> SK=RES#<ns>#<value>            # value read back after the ADD
          ConditionExpression: attribute_not_exists(SK)   # UNIQUE backstop
])
```

Subtlety: `ADD` returns the new value only via a plain `UpdateItem` (`ReturnValues=UPDATED_NEW`),
but a plain UpdateItem + separate Put is **not** atomic — a crash between them advances the
counter without an audit row (a *gap*, not a collision — acceptable for uniqueness/monotonicity,
but SLS-8 asks for **contiguous** reservation). To get contiguity **and** the audit row
atomically, do the counter `ADD` first (`UpdateItem`, capture new value), then a **conditional
`PutItem`** of the audit item; on the rare condition failure retry. If strict all-or-nothing is
required, wrap in `TransactWriteItems` (the counter increment is idempotent within the txn). The
composite-PK serialisation Postgres gives for free (invariant #2) is provided by DynamoDB's
**per-item atomic `ADD`** — two agents on the same counter item are serialised by DynamoDB, so
each gets a distinct increasing value. The `attribute_not_exists(SK)` conditional put is the
exact analogue of `UNIQUE(project_id, namespace, value)` (`models.py:363-367`).

**Optimistic locking / 412** (parity with `helpers.py:58-73`):

Every mutating task op is an `UpdateItem` with
`ConditionExpression "version = :expected"` and `ADD version :one`, **only when `If-Match` was
sent** (lenient path omits the condition, matching `helpers.py:62-64`). On
`ConditionalCheckFailedException` the adapter raises `VersionConflict` → **412**. Exact parity
with invariant #3.

### 3.4 Notes / 400KB item limit / counters / relations / idempotency

- **Notes are separate items** (`TASK#<pubid>#NOTE#<ts>`), never appended into the task item —
  this sidesteps the 400KB item cap entirely and keeps append O(1). Listing = `Query
  begins_with`. Own-item fields (status_note, proof_cmd) stay on the task item.
- **Counters** = single item per namespace, atomic `ADD` (§3.3).
- **Relations** = adjacency items; `supersedes` additionally flips the dst task
  (`tasks.py:364-366`) — that is a **two-item write → `TransactWriteItems`** (relation Put +
  dst-task conditional Update).
- **Idempotency** = `PutItem` with `ConditionExpression attribute_not_exists(SK)`; on
  ConditionalCheckFailed, `GetItem` the stored row and return it — exact analogue of the
  `IntegrityError` catch in `idempotency.py:84-91`.
- **Leases:** the one-active-lease invariant (`models.py:386-395`) is enforced *inline* by the
  claim's conditional write (owner-absent / lease-expired) — no separate lease lock item
  needed. Lease *history* is optional audit items with a TTL attribute for GC.

### 3.5 Jira integration + relations mirror (SLS-J1..J5 — realised, both backends)

The Jira feature (originally Postgres/ORM-only) was adapted to the storage port in the SLS-J*
epic, so it now has full Postgres/DynamoDB parity. No new GSI, table, migration, or reserved
number was needed — every addition rides the existing single-table partition by exact key or
`begins_with` range read.

**New DynamoDB item types** (also in the §3.1 table):

- **`RELIN` relation-mirror item (SLS-J2, decision D1).** DynamoDB has no cheap "incoming edges"
  query. Rather than add a GSI, `add_relation` writes a *second* mirror item
  `SK = TASK#<dst>#RELIN#<kind>#<src>` under the destination task, in the **same
  `TransactWriteItems`** as the forward `SK = TASK#<src>#REL#<kind>#<dst>` edge — so an edge and
  its mirror never diverge. `list_relations` is then two `begins_with` range reads on the task's
  own partition: `TASK#<ident>#REL#` (outgoing) + `TASK#<ident>#RELIN#` (incoming). The `RELIN`
  prefix does not alias `REL#` (the char after `REL` is `I`, not `#`), and `_load_task_full`
  ignores mirror items (it only collects `#COMMIT#`/`#NOTE#` children), so task loads are
  undisturbed. Postgres serves the same shape by reading its `task_relations` rows in both
  directions — no schema change.
  - **Backfill caveat (from DECISIONS.md, SLS-J2).** Relations written in production BEFORE the
    mirror item existed have a forward `REL#` item but **no** `RELIN#` mirror. Until a one-shot
    backfill (scan `type=relation` items, idempotently write the matching `relation_in_sk`
    mirror) runs, `GET .../relations` returns *outgoing* edges for old data but MISSES *incoming*
    edges for those pre-mirror relations. Tracked as a follow-up; not executed in SLS-J2.
- **`JIRACFG` singleton item (SLS-J3).** The per-project Jira config (`base_url`, `email`,
  encrypted API token, `jira_project_key`, `enabled`, `cached_transitions`) is one item per
  project at `PK = P#<slug>`, `SK = JIRACFG` (`keys.jira_config_sk()`). Create-once uses a
  conditional `attribute_not_exists(PK)` put → `Conflict`, mirroring the Postgres
  `UNIQUE(project_id)` backstop. The stored token is **always ciphertext** — the blueprint
  `encrypt()`s before the value crosses the storage port and decrypts (only for outbound Jira
  calls) after reading; the plaintext never enters storage, is never logged, and is never
  formatted into a SQL/DynamoDB expression. Project deletion removes it on both backends
  (Postgres FK `ON DELETE CASCADE`; Dynamo `delete_project` wipes the whole `P#<slug>`
  partition). Config update / `set_jira_transitions` are last-writer-wins read-then-write on both
  backends (no optimistic-lock version on the singleton — matches prior behaviour, a rare admin op).

**New storage-port methods** (on `StorageBackend`; both adapters implement each with identical
observable behaviour):

| Port method | Task | Parity note |
|---|---|---|
| `list_relations(slug, ident)` | SLS-J2 | Postgres: read `task_relations` both directions. DynamoDB: `begins_with` on `REL#` + `RELIN#`. Returns `[{direction, kind, task, created_at}]` from the requested task's perspective; unknown ident → `NotFound` (404) on both. |
| `get_jira_config(slug)` | SLS-J3 | Returns a `JiraConfigDTO` (ciphertext only) or `None` when unset; `NotFound` if the project is absent. |
| `create_jira_config(slug, data)` | SLS-J3 | Create-once: `Conflict` (409) if one already exists — PG `UNIQUE(project_id)`, Dynamo `attribute_not_exists(PK)`. |
| `update_jira_config(slug, data)` | SLS-J3 | Partial update; `NotFound` if project or config absent. Bumps `updated_at` on both. |
| `set_jira_transitions(slug, transitions)` | SLS-J3/J4 | Persists the cached transition map; used by the transition-cache warmup/refresh. Last-writer-wins on both. |
| `record_jira_sync(slug, task_ident, *, issue_key=None, error=None)` | SLS-J4 | Best-effort write-back of a sync result: sets `jira_issue_key` (when given) and `jira_sync_error` (the new value; cleared to `None` on success). **See D2 below.** |

**D2 — `record_jira_sync` is a SILENT write (hard rule, SLS-J4).** It updates ONLY the two Jira
task attributes and MUST NOT bump `task.version` and MUST NOT write a change-log (`Change`/delta)
entry — background sync metadata must never perturb optimistic-locking (`If-Match`/412) or the UI
delta feed. It MAY emit the existing `jira_sync_error` **audit event** (the `/events` path — which
is NOT the change-log delta feed). Parity is by construction: Postgres issues a **column-scoped
`UPDATE`** of just the two Jira columns (so a concurrent `version` bump is preserved); DynamoDB
uses a **scoped `UpdateItem`** (SET the two attrs, REMOVE `jira_sync_error` to clear on success)
rather than a full-item `PutItem`, so it never clobbers a concurrent write. Values bind via
`ExpressionAttributeValues`, never string-formatted. Asserted cross-backend in
`tests/test_record_jira_sync.py`: after the call `version` and `changes_head` are UNCHANGED on both
backends.

**Auto-sync + retry (SLS-J5).** Best-effort Jira sync is called from the create/complete task
blueprints after `storage.create_task` / `storage.complete_task`, so auto-sync fires through the
storage lifecycle on BOTH backends; it is a zero-outbound-call no-op (one cheap `get_jira_config`
read) when Jira is unconfigured/disabled. The manual retry endpoint enumerates candidates via
`list_tasks` + an in-memory filter (no new GSI) and re-syncs via the port — identical on both
backends. Sync is a synchronous outbound HTTP call on the hot path when Jira is enabled; the
async-offload alternative is filed as a follow-up.

---

## 4. Guarantee mapping — Postgres vs DynamoDB (side by side)

| Guarantee | Postgres primitive (file:line) | DynamoDB primitive | Needs Transaction? |
|---|---|---|---|
| **Atomic claim** (no double-claim) | `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` (`services.py:133-137`) | GSI1 candidate Query → conditional `UpdateItem` (`attribute_not_exists(owner) AND status=todo`), retry on `ConditionalCheckFailed` | No (single-item guard); claim+lease+event atomic only if bundled via TransactWriteItems |
| **Atomic reservation** (no dup number) | `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` + `UNIQUE(project,ns,value)` (`services.py:54-66`, `models.py:363-367`) | `UpdateItem ADD current_value :one` (serialised per-item) + conditional `PutItem attribute_not_exists(SK)` | **Yes** for contiguous+audit atomicity (TransactWriteItems) |
| **Optimistic lock** (If-Match/412) | `version` column + `check_if_match` (`helpers.py:58-73`) | `UpdateItem ConditionExpression version=:v; ADD version :one` → 412 on fail | No |
| **Complete task** (done + commit + lease close + event) | one SQL transaction (`tasks.py:236-253`) | task flip (authoritative conditional Update) + commit Put + lease-history + event | **Yes** if strict atomicity wanted; else task-flip authoritative, rest best-effort |
| **Supersedes** (relation + dst flip) | one transaction (`tasks.py:361-366`) | relation Put + dst-task Update | **Yes** (TransactWriteItems) |
| **Import spec** (bulk upsert) | one transaction (`services.py:164-219`) | `BatchWriteItem` in 25-item chunks (no conditions) | No — idempotent by key, non-atomic acceptable |
| **Cascade delete project** | FK `ON DELETE CASCADE` | Query all `P#<slug>` items → `BatchWriteItem` delete in chunks | No (adapter-driven) |

**Where DynamoDB cannot match Postgres without `TransactWriteItems`:** any operation writing
>1 item that must be all-or-nothing — **reserve (counter+audit)**, **complete (task+commit+lease+event)**,
**claim (task+lease+event)**, **supersedes (relation+dst)**. Recommendation: make the **single
authoritative item** carry the correctness guard (task status/owner/version, or the counter
`ADD`), and use `TransactWriteItems` where the audit/side-effect must be atomic with it. Costs:
`TransactWriteItems` is 2× WCU per item and caps at 100 items / 4MB per transaction — fine for
these small writes, but it is why `import_spec` stays on `BatchWriteItem` (idempotent) rather
than one giant transaction.

---

## 5. Config switch + test implications

**Config** (`app/config.py`): add
```python
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "postgres")   # postgres | dynamodb
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "spec-server")
DYNAMODB_ENDPOINT_URL = os.environ.get("DYNAMODB_ENDPOINT_URL")   # DynamoDB Local / LocalStack
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
```

**Factory** (`app/storage/__init__.py`):
```python
def make_storage(config) -> StorageBackend:
    backend = config["STORAGE_BACKEND"]
    if backend == "postgres":
        return PostgresBackend()                 # uses the existing db.session
    if backend == "dynamodb":
        return DynamoBackend(table=config["DYNAMODB_TABLE"],
                             endpoint_url=config.get("DYNAMODB_ENDPOINT_URL"),
                             region=config["AWS_REGION"])
    raise ValueError(f"unknown STORAGE_BACKEND {backend!r}")
```
Called in `create_app()` (`app/__init__.py:11-46`): `app.storage = make_storage(app.config)`.
Default stays `postgres` → **zero behaviour change locally**. `flask init-db` stays Postgres-only;
Dynamo table creation is Terraform (`infra`) or a `create-table` helper against DynamoDB Local.

**Tests:**
- The concurrency tests (no-collision claim, contiguous reservation, 412) must run against
  **both** backends — parameterise the `storage` fixture over `postgres` and `dynamodb`
  (SLS-8). This is the parity suite and the real proof.
- DynamoDB path uses **DynamoDB Local** (or LocalStack) via `DYNAMODB_ENDPOINT_URL`; add a
  `dynamodb-local` service to `docker-compose.yml` (SLS-7).
- DEC-3 (tests target real Postgres, not SQLite) stands: the Dynamo path must target real
  DynamoDB Local, not a mock — the guarantees are conditional-write-specific just as the
  Postgres ones are skip-locked-specific.

---

## 6. Refined SPEC task breakdown (SLS-2..SLS-11 + recommended additions)

Existing backlog maps well. **Reserve every migration number and every DynamoDB GSI/table
identifier via spec-keeper `POST /reservations` (namespaces `migration` and, new, `dynamo-gsi`)
— never pick them by reading max+1.** Note there is a live Alembic chain
(`migrations/versions/`) so a schema-touching Postgres task (e.g. any new column) needs a
reserved migration number.

| Task | Keep / change | Notes |
|---|---|---|
| **SLS-2** Extract StorageBackend + Postgres adapter | keep | verbatim behaviour lift; wire `app.storage` + `current_app.storage` in blueprints |
| **SLS-2.1** *(ADD / SPLIT)* Backend-neutral DTOs + errors | **add** | frozen DTOs replacing ORM dumps in `schemas.py`; `NotFound/Conflict/VersionConflict/BackendUnavailable` + error handlers. **Prerequisite** for both adapters; touches every `*Out` schema. Do before SLS-3. |
| **SLS-11** Config plumbing + factory | keep, **sequence after SLS-2** | factory + `STORAGE_BACKEND`; default postgres. Lands before any dynamo code is reachable. |
| **SLS-3.0** *(ADD / SPLIT from SLS-3)* Provision Dynamo table + GSIs | **add** | `infra/terraform/dynamodb.tf` (on-demand, PITR) + local `create-table` helper. **Reserve GSI index names via `reservations` namespace `dynamo-gsi`.** |
| **SLS-3** Dynamo adapter: tasks + claim-next | keep | GSI1 candidate query + conditional UpdateItem retry (§3.3) |
| **SLS-4** Dynamo adapter: reservations | keep | atomic `ADD` + conditional-put backstop; **call out TransactWriteItems for contiguity+audit** |
| **SLS-5** Dynamo adapter: optimistic-lock parity | keep | `version` ConditionExpression → 412 |
| **SLS-5.1** *(ADD, optional)* Multi-item atomicity (complete/supersedes) | **add or fold into SLS-3/6** | `TransactWriteItems` for complete (task+commit+lease+event) and supersedes (relation+dst). Explicit because it is the one place Dynamo ≠ Postgres. |
| **SLS-6** Dynamo adapter: notes/log/commits/relations/chains | keep | separate child items; GSI4 feeds; 400KB-safe notes |
| **SLS-7** Lambda entrypoint + local emulation | keep | WSGI adapter (`wsgi.py` exists); `dynamodb-local` in compose |
| **SLS-8** Adapter-parity test suite | keep | parameterise over both backends; **this is the proof** |
| **SLS-9** Docs (CLAUDE.md invariants + AGENTS_API.md) | keep | document the mapping table (§4) |
| **SLS-10** Optional data copy/sync tool | keep | enables switching without data loss (rollback aid) |

Recommended additions to file: **SLS-2.1** (DTOs/errors — hard prerequisite), **SLS-3.0**
(Terraform table + GSIs, reserve GSI names), **SLS-5.1** (TransactWriteItems atomicity —
or fold explicitly into SLS-3/SLS-4/SLS-6 acceptance criteria). Suggested ordering:
**SLS-2 → SLS-2.1 → SLS-11 → SLS-3.0 → SLS-3 → SLS-4 → SLS-5 → SLS-5.1 → SLS-6 → SLS-7 →
SLS-8 → SLS-9 → SLS-10.**

---

## 7. Risks / rollback / cost

**Risks / landmines**
- **DTO refactor blast radius (P1):** every `*Out` schema currently dumps ORM objects with lazy
  relationships (`schemas.py:159-171`). Missing an attribute name breaks a response silently.
  Mitigate with SLS-2.1 first + the parity suite.
- **GSI write amplification (P1 cost/perf):** ~5 GSIs; a claim writes GSI1 (status moves
  partition) + GSI2 (owner set) = 2 index updates per claim on top of the base write. Keep GSI
  projections to `KEYS_ONLY`/needed attributes to bound WCU.
- **Hot partition (P2):** all of a project's traffic lands on `PK=P#<slug>` (and its GSI1
  `...#ST#todo` partition). A single very busy project with many agents claiming concurrently
  can approach the 1000 WCU / 3000 RCU per-partition ceiling and throttle. Low risk at expected
  volume; future mitigation = shard the todo partition (`ST#todo#<0-N>`) and fan-out the claim
  query. Note now, don't build yet.
- **claim-next contention (P2):** under many simultaneous claimers, conditional-write retries
  waste RCU/WCU and add latency vs Postgres skip-locked's single round-trip. Bounded by
  candidate page size; acceptable.
- **GSI eventual consistency (P2):** "my specs" (`list_tasks?owner=`) right after a claim may
  lag briefly; correctness is unaffected (writes are conditional). Document it.
- **Reservation gap on partial failure (P2):** counter `ADD` then failed audit put leaves a gap.
  Use TransactWriteItems if contiguity is contractual (SLS-8).

**Cost**
- DynamoDB **on-demand** (per `infra/terraform/main.tf:33`) — pay-per-request, scales to zero,
  no idle cost; ideal for spiky agent traffic. At low volume this is cents/month. GSIs add
  per-write cost proportional to index count; the 5-GSI design roughly triples task-write cost
  vs a no-GSI table — still tiny in absolute terms for this workload.
- Lambda arm64, scales to zero (`main.tf:18-19`) — the switch enables a genuinely
  serverless/near-zero-idle deployment that Postgres (always-on RDS/container) cannot match.

**Rollback**
- The switch is a **config flip**: `STORAGE_BACKEND=postgres` reverts instantly with no schema
  change (DynamoDB is purely additive). Postgres remains the reference and default.
- SLS-10's copy/sync tool lets a deployment move data between backends before/after a switch,
  so a Dynamo trial is reversible without data loss.
- No `terraform apply` / deploy happens as part of this design task; the Dynamo table is created
  only when the coordinated infra change lands.

---

## 8. Confirmed vs assumptions

- **Confirmed (read in code):** all access patterns (§1.2) from every blueprint; the three
  primitives and their exact SQL (`services.py`, `helpers.py`, `idempotency.py`); ORM-dump
  coupling in schemas; Alembic migration chain exists; infra already targets DynamoDB
  single-table + GSIs + on-demand + PITR + arm64 Lambda (`infra/terraform/main.tf`).
- **Assumption (design choice, not verified against load):** ~5 GSIs suffice with no Scan — the
  `q` free-text filter and some list filters fall back to `FilterExpression` over a bounded
  candidate set (small backlogs), which is not a Scan but does over-read; if a project's backlog
  grows very large this may warrant a dedicated search path. Flagged, not blocking.
- **Not covered:** exact WCU/RCU numbers under real concurrency (needs the SLS-8 parity suite +
  load run); the precise `TransactWriteItems` vs best-effort trade-off per operation is left to
  SLS-5.1 acceptance criteria.
</content>
</invoke>
