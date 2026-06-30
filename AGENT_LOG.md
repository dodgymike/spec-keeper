# Agent Log

Append-only. One block per task/session. Pre-migration this is a flat file; the `LOG` epic moves it
to the server's `/events` endpoint.

## 2026-06-30 — EPIC MVP shipped (MVP-1 … MVP-7)

- Built the Spec Server MVP: Flask + flask-smorest + SQLAlchemy 2.0 + PostgreSQL, in Docker.
- Atomic primitives implemented and tested:
  - `claim-next` via `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` — concurrent claims never collide.
  - `reserve_number` via `INSERT ... ON CONFLICT (project_id, namespace) DO UPDATE ... RETURNING`
    with a `UNIQUE(project_id, namespace, value)` backstop — kills the "two agents grabbed 024" bug.
  - Optimistic locking via `tasks.version` + `If-Match` → 412.
- Verified end-to-end via curl (create project/epic/tasks → priority-ordered claim with no collision
  → owner filter → reservations 1,2,3 → complete with commit → If-Match 412).
- Test suite: `pytest -q` → **15 passed** against `specserver_test`.
- Chain note: this initial scaffold was built directly (greenfield), not through the per-task chain;
  the chain becomes mandatory for all subsequent tasks (PORT/LOG/HARDEN/DOGFOOD epics). Justification
  for the one-time skip: there was no prior code or backlog to claim against.

## 2026-06-30 — Post-MVP: git + PORT + LOG + HARDEN(partial)

- Initialized git; MVP committed on `main`; post-MVP work on branch `post-mvp-port-log-harden`.
- **PORT epic (committed `8bc5c96`):** `app/specmd.py` parser/renderer + `app/blueprints/ports.py`
  (`/import`, `/export`, `/export/diff`). Round-trip fidelity proven; validated on the real 568-line
  feed-reader `SPEC.md` (43 tasks, idempotent re-import).
- **LOG epic:** `Event` + `Decision` models, `app/blueprints/log.py` (`/events`, `/decisions`).
  Events auto-emitted on claim/complete/reserve/decision.
- **HARDEN (partial):** lease-expiry reaper (abandoned tasks re-claimable; stale lease retired) +
  `limit`/`offset` pagination on task/event lists.
- Tests: `pytest -q` → **25 passed** (15 MVP + 4 PORT + 4 LOG + 2 HARDEN).
- Still open: LOG-3 (chain tracking), HARDEN-1 (Alembic), HARDEN-3 (idempotency keys), DOGFOOD epic.
