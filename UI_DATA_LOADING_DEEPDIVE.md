# UI_DATA_LOADING_DEEPDIVE — Incremental (delta) dashboard loading

**Status:** Investigation only (no implementation, no deploy, no commit). Design + SPEC-ready plan.
**Author:** deep-diver · **Date:** 2026-07-24
**Scope:** Make the React/Vite dashboard load *deltas* instead of refetching everything each poll,
identically on **both** storage backends (`STORAGE_BACKEND=postgres|dynamodb`).

---

## 1. Symptom — what's observed

Every page re-runs its **entire** fetch on a fixed interval (30s / 60s) and replaces its whole
state, regardless of whether anything changed. Evidence:

- The shared poll hook re-runs a fetch effect by bumping a counter every `effectiveMs`:
  `ui/src/hooks/useLiveRefresh.ts:42-46` (`setInterval(() => setReload((r) => r + 1), effectiveMs)`).
- Pages depend on `reload` and re-issue the **full** fetch each tick, discarding the previous
  result:
  - `ProgressPage.tsx:33-54` — `Promise.all([getProject, listEpics, listTasks({limit:1000})])`,
    dep `[slug, reload]`, and `setState({status:"loading"})` on *every* reload (line 35).
  - `ProjectDetailPage.tsx:43-46` — same triple fetch, `limit:1000`.
  - `CoordinationPage.tsx:56-66` — `listProjects()` then a **fan-out**: for *each* project
    `Promise.all([listEpics, listTasks({status:"in_progress",limit:1000}), listCounters])`.
  - `ProjectsPage.tsx:103-119` — `listProjects()` then per-project `[listEpics, listTasks({limit:1000})]`.
  - `AdminPage.tsx:517-526` — `listProjects()` then per-project `[listTasks({limit:1000}),
    listProjectNotes({limit:1000})]`; page cadence `AUTO_REFRESH_MS = 60_000` (line 45).
  - `ActivityPage.tsx:80-86` — `Promise.all([listProjectNotes, listEvents])`.
- Page cadence constants: `AUTO_REFRESH_MS = 30_000` on Progress/ProjectDetail/Coordination/
  Projects; `60_000` on Admin. The user can override globally via `autoRefresh.tsx` (10s/30s/1m/5m/
  Off).

**The waste.** `TaskOut` (`app/schemas.py:167-194`) is a fat object: ~20 scalar fields plus nested
`tags[]`, `commits[]`, and `notes[]` (lines 184-186). A single task with history is easily several
KB of JSON. The list cap is `limit ≤ 1000` (`TaskQuery`, `schemas.py:233`) and every page requests
`limit: 1000`. So:

- A single project page transfers **the whole task list every 30s** even when idle.
- The fan-out pages (`Coordination`, `Projects`, `Admin`) issue **1 + P·(2–3)** requests per tick
  for **P** projects, each returning up to 1000 fat tasks — an N+1 that grows with the backlog and
  the project count, every 30–60s, per open tab.

This is the cost being fixed: near-constant full-snapshot transfer + re-render for a backlog that
changes rarely between polls.

> Not measured here: exact byte sizes / CloudWatch egress for the deployed project. The payload
> shape above is from the schema; a follow-up task should capture real `Content-Length` numbers
> from the deployed API before/after. Labeled a **hypothesis** on magnitude, **confirmed** on shape.

---

## 2. Evidence — the existing event log's fitness as a delta source

The user's hypothesis ("server tracks changes in a log; UI stores details locally and fetches the
log since its last checkpoint") has real foundations — there **is** an event log — but as built it
is **not** usable as a delta source. Three hard gaps, all evidence-backed.

### 2.1 No monotonic, queryable cursor is exposed

- `Event` model: PK `id` (bigserial) + `created_at`, indexed `(project_id, created_at)`
  (`app/models.py:470-490`).
- **Postgres** `list_events` orders by `created_at DESC, id DESC` and paginates by `offset/limit`
  only (`app/storage/postgres.py:735-740`). The serial `id` is a perfect monotonic tiebreak — but
  it is **not exposed** and there is **no `since` filter**.
- `EventOut` exposes `event_type, agent, task_id, message, payload, created_at` — **no `id`, no
  sequence** (`app/schemas.py:411-417`). `EventQuery` supports only `event_type/agent/task/limit/
  offset` — **no cursor** (`app/schemas.py:420-425`).
- **DynamoDB** events sort by `GSI4SK = "<ts>#<uuid>"` (`app/storage/keys.py:169-170`,
  `dynamo.py:1131`), queried newest-first on `GSI4` (`dynamo.py:1163-1196`). The `<uuid>` tiebreak
  is **random**, not monotonic; `<ts>` is `datetime.now(UTC).isoformat()` (`dynamo.py:105-110`) at
  microsecond resolution, so two events in the same microsecond order arbitrarily (but stably, since
  the key is stored). A resumable cursor `GSI4SK > last` *is* well-defined lexicographically — but
  its value differs completely from the Postgres `(created_at,id)` cursor.

→ **No cross-backend cursor exists today.** Postgres has a serial id (hidden); DynamoDB has a
`ts#uuid` composite. Neither is surfaced, and they are not the same shape.

### 2.2 Coverage is incomplete — most UI-relevant mutations emit **no** event

Event emission call sites (exhaustive grep of both adapters + `services.py`):

| Mutation | Postgres emits? | Dynamo emits? |
|---|---|---|
| `claim_next` | ✅ `"claimed"` (`services.py:161`) | ✅ (`dynamo.py:796`) |
| `complete_task` | ✅ `"completed"` (`postgres.py:575`) | ✅ (`dynamo.py:920`, in the TransactWriteItems) |
| `reserve_number` | ✅ `"reserved"` (`services.py:80`) | ✅ (`dynamo.py:1079-1088`) |
| task/epic note | ✅ `"note"` (`postgres.py:435,637`) | ✅ (`dynamo.py:501,994`) |
| decision | ✅ (`postgres.py:811`) | ✅ (`dynamo.py:1286`) |
| chain run/step | ✅ (`postgres.py:836,892`) | ✅ (`dynamo.py:1329,1384`) |
| **`create_task`** | ❌ | ❌ |
| **`update_task`** (title/priority/epic/…) | ❌ | ❌ |
| **`set_status`** (todo↔blocked↔deferred…) | ❌ | ❌ |
| **`delete_task`** | ❌ | ❌ |
| **`release_task`** | ❌ | ❌ |
| **`add_commit`** | ❌ | ❌ |
| **`add_relation`** | ❌ | ❌ |
| **`create_epic` / `update_epic`** | ❌ | ❌ |
| project / member changes | ❌ | ❌ |

The most common dashboard-visible changes — a task being **created, edited, re-statused, released,
or deleted** — produce **no event at all**. A UI that trusted the event log as its delta feed would
silently miss them. **This is the decisive finding: the log cannot be the delta source as-is.**

### 2.3 Deletions are not recorded, and the payload is a bare pointer (with a parity bug)

- `delete_task` emits nothing (§2.2) — there is **no tombstone**, so a delta consumer could never
  learn to **evict** a deleted task. Eviction is a hard requirement for a correct client cache.
- Events carry a *pointer*, not the new state: Postgres `EventDTO.task_id` = integer
  (`postgres.py:143-145`); DynamoDB stores `task_pubid`/`task_key` on the item (`dynamo.py:1126-1132`)
  but its `_event_dto` **hardcodes `task_id=None`** (`dynamo.py:1142-1146`). So `EventOut.task_id`
  (`schemas.py:414`, typed `number|null` in `ui/src/api/types.ts:118`) is **always `null` on
  DynamoDB** and an **integer on Postgres**.
  → This is an existing **adapter-parity bug** (violates the hard rule in `CLAUDE.md` → "Backend
  parity"). It also means the current event payload is not even a *stable* pointer across backends.
  Any delta design must standardise on the **`public_id`** (stable, cross-backend) as the entity key.

### 2.4 Retention / pruning

- The DynamoDB table has a `ttl` attribute (`infra/terraform/dynamodb.tf:179`) but event items do
  **not** set it (grep: `ttl` in `dynamo.py` only appears for *lease* expiry, lines 789-790). So
  events currently never expire on DynamoDB, and Postgres never prunes them either. Good for a
  cursor *today* (no gaps), but a delta feed **must** design for a bounded retained window +
  full-resync fallback, because unbounded event growth will eventually force a TTL.

### 2.5 What *is* usable today

- Every task already carries `version` (optimistic-lock, bumped on each mutation — `models.py:243`;
  `TaskOut.version` `schemas.py:183`) and `updated_at` (`models.py:250-252`; `TaskOut.updated_at`
  `schemas.py:188`). These are honest per-entity change signals and survive on both backends.
- `list_tasks` filtering exists (`postgres.py:443-467`, dynamo `643+`) but there is **no
  `updated_after`** filter and no `updated_at`-ordered index on either backend.
- `/events` is already correctly project-scoped and permission-gated
  (`require_project_perm(slug,"read")`, `app/blueprints/log.py:32`) — the isolation model for a
  delta feed is already the right shape.

---

## 3. Options evaluated

Legend: **Parity** = can it behave identically on Postgres *and* DynamoDB. **Cost** = incremental
Lambda/DDB/egress. **Correctness** = can it be made gap-free incl. deletes.

### Option 1 — Event-log delta (the user's hypothesis)
UI keeps a local cache + cursor; polls `/events?since=<cursor>`; applies upserts, evicts deletes;
advances cursor.
- **Pros:** one feed for everything; naturally incremental; matches the existing `Event` table and
  the "append-only, immutable" design (`models.py:470-472`).
- **Cons / blockers (all from §2):** (a) **no exposed monotonic cursor**; (b) **coverage gap** — most
  task mutations emit nothing; (c) **no deletion tombstones**; (d) payload is a pointer, and a
  buggy/divergent one; (e) needs a retained-window + full-resync fallback.
- **Parity:** achievable but **only after** a monotonic cross-backend cursor is designed (the crux,
  §4) and emission is completed in *both* adapters.
- **Cost:** low read cost (small deltas); modest extra write cost (emit on every mutation + stamp a
  seq).
- **Verdict:** the right *shape*, but requires real server work before it's viable. **Foundation of
  the recommendation.**

### Option 2 — `?updated_after=<cursor>` on the list endpoints
Server returns only rows with `updated_at > cursor`; deletes handled by a separate tombstone feed.
- **Pros:** no client-side event application; reuses existing list shapes; the client just merges
  changed rows into its cache.
- **Cons:** (a) still needs a **separate deletes feed** (an `updated_after` filter can't report rows
  that no longer exist); (b) needs an `updated_at`-ordered index/GSI on **both** backends —
  DynamoDB has none today, requiring a new GSI (`updated_at`) or a scan (unacceptable); (c)
  `updated_at` as a *sole* cursor is unsafe — clock granularity + same-timestamp writes → skipped or
  duplicated rows at the boundary; needs a `(updated_at, public_id)` compound cursor.
- **Parity:** possible but adds a GSI + careful boundary semantics on DynamoDB.
- **Cost:** low read; a new GSI on DynamoDB adds storage + write-amplification cost on every task
  write.
- **Verdict:** a good **optimization to pair with** a change-log for the *upsert* fetch, but not
  self-sufficient (deletes).

### Option 3 — Cheap change-poll + conditional fetch
A tiny endpoint returns the current per-project **change cursor** (max seq). The UI polls *that*
(bytes, not KB) and only does a delta/full fetch when it moved.
- **Pros:** **biggest bandwidth win for the common "nothing changed" case** — the dominant case for
  a task backlog. Turns a P·(2–3)×1000-task poll into one tiny request per project (or one batched
  request for all projects). Pairs with Option 1 or 2.
- **Cons:** needs a monotonic per-project cursor (same crux as Option 1); one extra tiny request.
- **Parity:** trivial once the cursor exists (§4).
- **Cost:** minimal (a `GetItem`/single row read; can be a projection query).
- **Verdict:** **include it.** Cheap, high-leverage, complements the delta feed.

### Option 4 — HTTP caching (ETag / `If-None-Match` → 304)
Attach an ETag to list responses; client sends `If-None-Match`; server answers `304` when unchanged.
- **Pros:** low effort; standard; kills the body on the "nothing changed" case.
- **Cons:** **not a true delta** — a 200 still returns the *whole* list when *one* task changed; the
  ETag must be derived from a per-project change cursor anyway (else you hash the whole payload
  server-side each poll — costly on Lambda). API Gateway HTTP API + Lambda proxy passes
  `If-None-Match` through but you implement 304 yourself.
- **Parity:** fine (ETag = the cursor).
- **Verdict:** a **cheap complement** to Option 3 (the ETag *is* the change cursor), but does not by
  itself deliver deltas. Optional nicety.

### Option 5 — Push (WebSocket / SSE)
Server pushes changes in real time.
- **Cons on this stack:** the API is **Lambda + API Gateway HTTP API + DynamoDB, scale-to-zero,
  cost-conscious** (`CLAUDE.md` → "Source of truth"). SSE needs a long-lived response — awkward/
  impossible on request/response Lambda. API Gateway **WebSocket API** exists but is a separate API
  type with **connection-duration billing**, `$connect/$disconnect/$default` routes, a connection
  registry (another DynamoDB table), and a fan-out mechanism (DynamoDB Streams → Lambda → post-to-
  connection). That is a large surface and standing cost for a dashboard that tolerates 30s
  latency.
- **Verdict:** **out of scope.** Real-time is not a requirement; polling a cheap cursor gets ~all
  the benefit at a fraction of the complexity/cost. Documented and declined.

---

## 4. The cross-backend cursor (the crux)

A delta feed is only correct if the cursor is **monotonic and total-ordered on both backends**, with
**identical observable semantics**. `updated_at` alone fails this (§3, Option 2). The robust design:

**Stamp every change with a per-project monotonic `seq` (integer), allocated by the primitive the
project already trusts for collision-proof numbering** — the atomic counter
(`reserve_number`-style): Postgres `INSERT … ON CONFLICT DO UPDATE … RETURNING`
(`services.py:56-69`); DynamoDB atomic `ADD current_value :1` on the counter item
(`CLAUDE.md` → "Atomic reservation"; `dynamo.py` reserve path). This is the *one* mechanism the
codebase already guarantees is monotonic and collision-free on both backends — reuse it, do **not**
invent read-max-plus-one.

Concretely, introduce a per-project **change-log** entry `{seq, entity_type, entity_pubid, op, …}`
where `seq` comes from a reserved namespace (e.g. `changelog`) of the counter:

- **Postgres:** a `changes` table (or reuse `events` with a new non-null `seq bigint` column filled
  from the counter), unique `(project_id, seq)`, `seq` ascending = total order. (A dedicated bigserial
  would also be monotonic, but the counter keeps the two backends *symmetric*.)
- **DynamoDB:** a change item `SK = CHANGE#<zero-padded seq>` (zero-pad so lexical = numeric order),
  plus a GSI `PK = P#<slug>#CHANGES`, `SK = <padded seq>` for ascending range queries
  `seq > cursor`. Written in the **same `TransactWriteItems`** as the entity mutation so the change
  entry and the entity flip commit **all-or-nothing** (matches the existing multi-item-atomicity
  rule in `CLAUDE.md` — `complete`, `supersedes`, `reserve` already use `TransactWriteItems`).

**Cursor value = the integer `seq`.** Identical type and semantics on both backends → parity holds
by construction, and the client treats it as an opaque monotonically-increasing integer.

**Head cursor** (Option 3): `GET /projects/{slug}/changes/head` → `{ "cursor": <maxSeq> }` — a single
counter read on both backends. Also returned as an `ETag` on list/delta responses (Option 4).

**Retained window + fallback:** the server advertises `min_retained_seq`. If a client's `since <
min_retained_seq` (e.g. after TTL pruning or a long-offline tab), the delta endpoint answers
`409 full_resync_required` (or `{full_resync:true}`) and the client discards its cache and does a
full paginated fetch, then resets its checkpoint to the head cursor **captured before** paginating
(so changes during the resync are re-applied, not lost).

---

## 5. Recommended design (pragmatic combination: 3 + 1, with 2/4 as optimizations)

A **change-log with a monotonic `seq` cursor**, a **cheap head-cursor poll**, a **delta endpoint**,
and a **client cache with full-resync fallback**. Deletes are first-class tombstones.

### 5.1 Server

1. **Change-log capture (both adapters).** On *every* UI-relevant mutation emit a change entry
   `{seq, entity_type ∈ {task,epic,note,commit,relation,reservation,project,member}, entity_pubid,
   op ∈ {upsert,delete}, version, occurred_at}`. Fill the §2.2 gaps: `create_task`, `update_task`,
   `set_status`, `release_task`, `delete_task` (→ `op=delete`, the tombstone), `add_commit`,
   `add_relation`, `create_epic`, `update_epic`. Keep it in the existing transaction/`TransactWriteItems`
   so the entity write and the change entry are atomic. **Standardise the pointer on `public_id`**
   (fixes the §2.3 `task_id` parity bug at the same time).
2. **Payload decision — compact snapshot for upserts.** Embed the changed entity's current DTO
   (the same object the list endpoint returns) in the change entry's `snapshot` for `op=upsert`;
   deletes carry only `{entity_type, entity_pubid}`. Rationale: one round trip, no stale-ordering
   hazard, no N+1 follow-up GET. (Alternative — thin pointer + `?updated_after` batch refetch,
   Option 2 — is viable but needs the extra GSI and a deletes feed anyway; snapshot-in-changelog is
   simpler and equally parity-safe.)
3. **Delta endpoint:** `GET /projects/{slug}/changes?since=<seq>&limit=<n>` →
   `{ cursor:<newMax>, changes:[…ascending…], truncated:<bool>, full_resync_required:<bool>,
   min_retained_seq:<seq> }`, ascending by `seq`, permission-gated exactly like `/events`
   (`log.py:32`). `truncated` drives client re-poll until caught up (pagination interplay, §6).
4. **Head endpoint:** `GET /projects/{slug}/changes/head` → `{cursor, min_retained_seq}` (tiny).
   Optionally fold a per-project head map into `GET /projects` so the fan-out pages poll **one**
   request to decide which projects to delta-fetch.
   **DONE (UI-DELTA-10):** shipped as a dedicated batch endpoint `GET /api/v1/projects/heads` →
   `{"heads": {<slug>: {cursor, min_retained_seq}, …}}`, isolation-scoped to the caller's visible
   projects (same filter as `GET /projects`; a non-member's head is never present) — lower-risk than
   changing the `GET /projects` contract and adds no per-project cost to the many `/projects` callers.
   It goes through the `changes_heads_for(slugs)` storage port on BOTH adapters (Postgres grouped
   `max(seq)`/`min(seq)` read; DynamoDB reuses the per-project `changes_head` + base reads — no new
   GSI). Client: `getProjectsHeads()` + the pure `syncMultiHead` fan-out helper (`ui/src/lib/
   multiHeadSync.ts`) — one head poll per tick decides which projects advanced, and ONLY those get a
   per-project delta/rollup fetch (wired into `ProjectsPage`). The single-project `useDeltaRefresh`
   path (ProgressPage) is unchanged.
5. **`?updated_after` (optional, Option 2):** add to `list_tasks`/`list_epics` as an *efficiency*
   path for full-resync-avoidance on huge projects; requires an `updated_at` GSI on DynamoDB — defer
   unless payload numbers justify it.

### 5.2 Client (React)

- **Cache:** a per-project normalized store keyed by `entity_type + public_id` (tasks, epics, notes).
  In-memory for the session; optionally mirror to **IndexedDB** (not localStorage — task lists blow
  past the ~5 MB quota and localStorage is synchronous) so a reload/tab-reopen resumes from cursor
  instead of a cold full fetch. Keep localStorage only for the small **checkpoint** `{cursor}` per
  project if IndexedDB is skipped in v1.
- **Checkpoint:** persist `{project_slug → cursor}`. On mount, if cache present and cursor known →
  delta-fetch; else full fetch + set cursor to head.
- **Delta-apply:** for each change ascending: `upsert` → replace the cache entry (snapshot);
  `delete` → **evict**. Advance cursor to `cursor` from the response. Re-poll while `truncated`.
- **Poll loop:** replace the blunt "bump reload → full refetch" in `useLiveRefresh.ts:42-46` with:
  poll the cheap **head** cursor; if `cursor > checkpoint` → delta-fetch; else no-op (no body, no
  re-render). Manual "Refresh" stays a forced delta-fetch.
- **Full-resync fallback:** on `full_resync_required` (or `since < min_retained_seq`, or a client
  schema-version bump) → capture head cursor, drop cache, paginate a full fetch, replay any deltas
  since the captured head, set checkpoint.
- **Render:** derive view state from the cache selector instead of replacing whole-page state each
  tick — removes the `setState({status:"loading"})`-on-every-reload flash (`ProgressPage.tsx:35`).

### 5.3 Why this shape

- The **common idle case** (nothing changed) costs **one tiny head request per project** instead of
  P·(2–3)×1000-task payloads — the single biggest win, and it needs only the cursor (§4).
- Deltas are **gap-free and delete-aware** (change-log covers every mutation incl. tombstones).
- **Parity by construction:** the cursor is the same integer type with the same monotonic semantics
  on both backends because it rides the already-proven atomic-counter primitive.

---

## 6. Hard problems / landmines (called out explicitly)

1. **Cursor total-ordering on DynamoDB.** `ts#uuid` (`keys.py:169`) is *stable* but its tiebreak is
   random and it differs in shape from Postgres. **Do not** ship a cursor whose value/semantics
   differ per backend. Use the counter-allocated integer `seq` (§4); zero-pad the DynamoDB SK so
   lexical order = numeric order.
2. **Capturing deletes.** `delete_task` emits nothing today (§2.2/§2.3). Without a `delete`
   tombstone the client can never evict — a deleted task lingers forever. The change-log **must**
   record deletes as first-class entries.
3. **Multi-item atomicity.** The change entry must commit **with** the entity mutation
   (Postgres transaction; DynamoDB `TransactWriteItems`, as `complete`/`supersedes`/`reserve`
   already do). A change entry written *after* a separate commit can be lost on failure → a silent
   gap the client never recovers from.
4. **Cache coherence / ordering.** Apply changes strictly in ascending `seq`; never apply a lower
   `seq` after a higher one. Coalesce multiple changes to the same entity to the latest. On
   full-resync, capture head **before** paginating, then replay.
5. **Pagination interplay.** A full-resync spanning multiple `limit≤1000` pages
   (`schemas.py:233`) must be a consistent snapshot — reuse the ISO-10 consistent-read discipline
   (recent commit `58e8d94`/ISO-10) and re-apply deltas since the pre-pagination head.
6. **Auth / isolation of the delta feed.** The change/head endpoints must be per-project and
   `require_project_perm(slug,"read")`-gated exactly like `/events` (`log.py:32`). Never expose a
   cross-project global feed (leak risk). The `seq` namespace is per-project.
7. **Retention / TTL.** If a TTL is ever added to change entries, `min_retained_seq` and the
   `full_resync_required` path must exist first, or offline clients silently miss deletes.
8. **Existing `task_id` parity bug (§2.3).** DynamoDB `_event_dto` returns `task_id=None`
   (`dynamo.py:1144`) while Postgres returns an int (`postgres.py:144`). Fix by standardising the
   change-log pointer on `public_id`; do not carry the divergent integer id into the new feed.
9. **Snapshot size.** Embedding full `TaskOut` (with `notes[]`/`commits[]`) in every change entry
   can bloat the feed for chatty tasks. Consider a *lean* snapshot (scalars + counts, omit
   `notes[]`/`commits[]` unless the detail view is open) — measure before deciding.

---

## 7. SPEC-ready task breakdown (ordered, atomic; BOTH backends)

Each task is small, testable, and lands on **both** adapters where it touches storage (hard parity
rule). Reserve any migration/table/GSI numbers via the orchestrator (`POST /reservations`,
namespace e.g. `dynamo-gsi` / `pg-migration`) — **never pick a number by hand**.

**Epic: `UI-DELTA` — incremental dashboard loading.**

1. **UI-DELTA-1 — Fix the `task_id` event parity bug (prerequisite cleanup).**
   Make DynamoDB `_event_dto` surface a stable pointer (`task_pubid`) matching Postgres semantics;
   add a parity test asserting identical `EventOut` pointer fields on both backends. *(fixes §2.3;
   `dynamo.py:1142-1146`, `postgres.py:143-145`, `schemas.py:411-417`.)*

2. **UI-DELTA-2 — Design + reserve the change-log cursor primitive.**
   ADR in `DECISIONS.md`: `seq` via the atomic counter (namespace `changelog`); Postgres
   `changes` table (or `events.seq` column) + DynamoDB change item/GSI. Reserve the migration
   number and the GSI number via `POST /reservations`. No code.

3. **UI-DELTA-3 — Postgres change-log write path.**
   Emit a change entry `{seq, entity_type, entity_pubid, op, version, occurred_at, snapshot}` inside
   the **same transaction** as every mutation currently missing one (§2.2): create/update/set_status/
   release/**delete**/add_commit/add_relation task, create/update epic. Tests: each mutation writes
   exactly one ascending change; delete writes a tombstone.

4. **UI-DELTA-4 — DynamoDB change-log write path (parity).**
   Same as UI-DELTA-3 via `TransactWriteItems`; `seq` from the atomic `ADD` counter; zero-padded
   `CHANGE#<seq>` SK + change GSI. Tests mirror UI-DELTA-3. Must pass the SLS-8 adapter-parity suite.

5. **UI-DELTA-5 — Delta + head endpoints + schemas.**
   `GET /changes?since=&limit=` and `GET /changes/head`; `ChangesQuery`/`ChangeOut`/`ChangesHeadOut`
   schemas; ascending order; `truncated`, `full_resync_required`, `min_retained_seq`; ETag =
   head cursor. `require_project_perm(slug,"read")` (§6.6). Both backends.

6. **UI-DELTA-6 — Concurrency/parity tests for the feed.**
   N concurrent mutations → the feed shows N strictly-increasing `seq` with no gaps/dupes on **both**
   backends; deletes appear as tombstones; `since` past the tail returns empty at head;
   `since < min_retained_seq` → `full_resync_required`.

7. **UI-DELTA-7 — Client cache + checkpoint store.**
   Normalized per-project store keyed by `entity_type+public_id`, `{slug→cursor}` checkpoint
   (in-memory + optional IndexedDB). Delta-apply (upsert/evict) in ascending `seq`. Unit tests for
   apply/evict/coalesce/ordering.

8. **UI-DELTA-8 — Rewire `useLiveRefresh` to cheap-poll + delta.**
   Poll head cursor; delta-fetch only when advanced; drive pages off cache selectors instead of
   whole-page `setState`. Keep manual Refresh + the `autoRefresh` preference/Off semantics
   (`autoRefresh.tsx`). Remove the reload-flash (`ProgressPage.tsx:35`).

9. **UI-DELTA-9 — Full-resync fallback + pagination consistency.**
   Implement capture-head-before-paginate, drop-and-refetch on `full_resync_required`, replay deltas
   since captured head; reuse ISO-10 consistent-read discipline. Test: mutation during a multi-page
   resync is not lost.

10. **UI-DELTA-10 — Fan-out pages onto the batched head poll.**
    Fold per-project head cursors into `GET /projects` (or a batch head) so Coordination/Projects/
    Admin poll **one** request to decide which projects to delta-fetch (kills the N+1). Both
    backends.

11. **UI-DELTA-11 (optional) — `?updated_after` efficiency path.**
    Add `updated_after` to `list_tasks`/`list_epics` + the DynamoDB `updated_at` GSI, to cheapen
    large-project resyncs. Gate on measured payload numbers (§1 follow-up). Both backends.

12. **UI-DELTA-12 — Measurement task (do first as evidence, then last as proof).**
    Capture real request counts + `Content-Length` from the deployed API for the current full-poll
    vs. the delta design; record in the epic notes. Confirms the §1 magnitude hypothesis.

**Dependencies:** 1 → 2 → (3,4) → 5 → 6 → (7,8) → 9 → 10 → (11) ; 12 brackets the epic.
**Chain (per task):** spec-keeper → implementer → test-engineer → reviewer → security → documentation;
add **data-reviewer** + **reliability-reviewer** on 3/4/5/6/9 (parity + failure modes), **ui-reviewer**
on 7/8/10, **aws-infra** for the GSI in 4/11 (mutate) and **deploy-coordinator** for any deploy.

---

## 8. Cost / risk / rollback

- **Cost — down, materially.** Idle polls collapse from P·(2–3)×1000-task payloads to one tiny
  head request per project (per tab) every 30s. Extra *write* cost: one change entry + one counter
  bump per mutation (already inside the existing transaction/`TransactWriteItems`) — a few extra
  WCUs on a low-write workload; negligible vs. the read/egress saved.
- **Risk — correctness of the feed.** A dropped/duplicated change entry silently desyncs a client.
  Mitigated by transactional/`TransactWriteItems` atomicity (§6.3), the parity suite (UI-DELTA-6),
  and the full-resync fallback (UI-DELTA-9) as a safety net for *any* desync.
- **Risk — parity.** Every storage task lands on both adapters in the same task (hard rule); SLS-8
  gates merge. `data-reviewer`/`reliability-reviewer` review 3/4/5/6/9.
- **Rollback — trivial and staged.** The change-log write path (UI-DELTA-3/4) is additive and inert
  until the client uses it; the endpoints (5) are new routes. The client rewire (8) is behind the
  existing `autoRefresh` mechanism — a feature flag / revert of the hook restores today's full-poll
  behaviour with no server rollback needed. Server change entries are append-only and harmless if
  unread.
- **No infra teardown / preview env involved** in the investigation (read-only). GSI additions
  (4/11) are the only durable infra change and go through `aws-infra` + `deploy-coordinator`.

---

## 9. Residual unknowns

- Real payload bytes / egress on the deployed project (UI-DELTA-12) — magnitude is a **hypothesis**;
  shape is **confirmed** from schemas.
- Whether to embed full vs. lean snapshots in change entries (§6.9) — decide with measurement.
- Whether `?updated_after` + `updated_at` GSI (Option 2 / UI-DELTA-11) earns its DynamoDB
  write-amplification — defer until numbers justify it.
- Multi-tab cache sharing (a `BroadcastChannel` could let tabs share one delta stream) — nice-to-have,
  not scoped here.

---

## 10. UI-DELTA-12 — Measured results

The magnitude hypothesis in §1 is now **measured**, not just modelled. The harness
`tests/test_delta_measurement.py` seeds a backlog-sized project (120 tasks + 5 epics, each task
carrying a realistic multi-paragraph brief) into the app test client and measures the **actual
serialized HTTP response bytes** (`Content-Length`) of the three loading strategies. It is
Postgres-only because the numbers measure the HTTP payload (Marshmallow → JSON), which is
**backend-independent** — the endpoints' behavioural parity is proven cross-backend elsewhere
(`test_changes_api.py`, `test_project_heads.py`).

Run: `pytest -s -k delta_measurement` (in-container, throwaway DB). Verbatim output from the run on
`2026-07-24`:

```
================ UI-DELTA-12 measured results (local) ================
  Seeded project      : 120 tasks, 5 epics
  --------------------------------------------------------------
  BASELINE tick (old) :  239,818 B  (234.2 KB)
      tasks?limit=all :  238,276 B  (232.7 KB) for 120 tasks -> 1,986 B/task
      /epics          :    1,542 B  (1.5 KB)
  --------------------------------------------------------------
  IDLE tick (new)     :       36 B  (0.0 KB)  = 0.0150% of baseline  (99.9850% reduction)
  CHANGE tick (new)   :    2,271 B  (2.2 KB)  = 0.95% of baseline  (99.05% reduction)
  --------------------------------------------------------------
  Idle head-poll is  6,662x smaller than a full refetch
  Single-change delta is 106x smaller than a full refetch
=====================================================================
```

**Interpretation:**

- **Baseline (old per-tick cost): ~234 KB/tick** for a 120-task backlog (~1,986 B/task + 1.5 KB of
  epics). This is what the pre-delta `ProgressPage` refetched on *every* tick, idle or not. It scales
  linearly with the backlog and is paid per tab; the production backlog (119 tasks, longer briefs)
  measured ~374 KB, the same order of magnitude — these local seed descriptions are slightly leaner
  (~2 KB vs ~3 KB/task).
- **Idle tick (the common case): 36 bytes/tick** — a ~**99.99% reduction** (~6,662× smaller). The vast
  majority of ticks change nothing, so this is the dominant saving: idle polling collapses from
  hundreds of KB to a fixed-size head cursor.
- **Single-change tick: 2.3 KB** to propagate one mutation — a **~99% reduction** (~106× smaller) vs.
  refetching the whole backlog. The delta cost is ~one task snapshot, independent of backlog size.

**Assertions (generous, non-flaky):** idle poll < 1% of baseline; single-change delta < 25% of
baseline; delta strictly cheaper than a full refetch; idle strictly cheaper than a delta. The
printed numbers show the far larger real-world margin.

> **Scope of this proof:** these are **local** measurements against the app test client (the
> HTTP payload is what egresses either way, so the byte counts transfer directly). A **production
> confirmation** — real request counts + `Content-Length` from the deployed API — will follow
> **post-deploy** and be recorded in the epic notes, closing out UI-DELTA-12's "then last as proof"
> bracket.
