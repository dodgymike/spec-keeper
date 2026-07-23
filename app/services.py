"""Transactional service helpers for the concurrency-critical operations.

These encapsulate the two patterns that make the server collision-proof:

* ``reserve_number`` — atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``
  upsert on the ``counters`` table. The composite primary key serialises
  concurrent callers at the row level, so each gets a distinct, monotonically
  increasing value. (This kills the "two agents both grabbed 024" bug.)

* ``claim_next_task`` — ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` dequeue.
  Concurrent claimers never see the same row; they skip locked rows and take
  the next, so N agents calling at once receive N distinct tasks (or none).
"""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from flask import current_app

from .extensions import db
from .models import (
    CLAIMABLE_STATUSES,
    Epic,
    Event,
    Lease,
    LeaseState,
    Priority,
    Reservation,
    Task,
    TaskStatus,
    utcnow,
)
from .specmd import ParsedSpec, validate_parsed_task


def log_event(project_id: int, event_type: str, agent=None, task_id=None,
              message=None, payload=None) -> Event:
    """Append an immutable event to the project's stream."""
    event = Event(
        project_id=project_id, event_type=event_type, agent=agent,
        task_id=task_id, message=message, payload=payload or {},
    )
    db.session.add(event)
    return event

# Lower index == higher priority. Tasks with no priority sort last.
_PRIORITY_ORDER = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3}


def reserve_number(project_id: int, namespace: str, reserved_by=None,
                   task_id=None, note=None) -> Reservation:
    """Atomically allocate the next number in ``namespace`` for the project."""
    stmt = (
        sa.text(
            """
            INSERT INTO counters (project_id, namespace, current_value)
            VALUES (:pid, :ns, 1)
            ON CONFLICT (project_id, namespace)
            DO UPDATE SET current_value = counters.current_value + 1
            RETURNING current_value
            """
        )
        .bindparams(pid=project_id, ns=namespace)
    )
    value = db.session.execute(stmt).scalar_one()

    reservation = Reservation(
        project_id=project_id,
        namespace=namespace,
        value=value,
        reserved_by=reserved_by,
        task_id=task_id,
        note=note,
    )
    db.session.add(reservation)
    log_event(project_id, "reserved", agent=reserved_by, task_id=task_id,
              message=f"reserved {namespace} #{value}",
              payload={"namespace": namespace, "value": value})
    db.session.flush()
    return reservation


def _priority_sql_order():
    """SQL ORDER BY fragment: P0..P3 first (in order), NULL priority last."""
    return sa.case(
        (Task.priority == Priority.P0, 0),
        (Task.priority == Priority.P1, 1),
        (Task.priority == Priority.P2, 2),
        (Task.priority == Priority.P3, 3),
        else_=9,
    )


def claim_next_task(project_id: int, agent: str, epic_id=None,
                    priority_max: Priority | None = None,
                    component: str | None = None,
                    lease_ttl: int | None = None) -> Task | None:
    """Atomically claim the next claimable (todo) task. Returns the task, or
    None if none are available. The row is locked with FOR UPDATE SKIP LOCKED
    so concurrent callers never collide."""
    now = utcnow()
    claimable = [s for s in CLAIMABLE_STATUSES]
    # A task is claimable if it is todo and unowned (or owned by us), OR it is
    # in_progress but its lease has expired (the reaper path — an abandoned task
    # returns to the pool automatically).
    query = (
        sa.select(Task)
        .where(Task.project_id == project_id)
        .where(
            sa.or_(
                sa.and_(
                    Task.status.in_(claimable),
                    sa.or_(Task.owner.is_(None), Task.owner == agent),
                ),
                sa.and_(
                    Task.status == TaskStatus.in_progress,
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at < now,
                ),
            )
        )
    )
    if epic_id is not None:
        query = query.where(Task.epic_id == epic_id)
    if component is not None:
        query = query.where(Task.component == component)
    if priority_max is not None:
        cutoff = _PRIORITY_ORDER[priority_max]
        allowed = [p for p, idx in _PRIORITY_ORDER.items() if idx <= cutoff]
        query = query.where(Task.priority.in_(allowed))

    query = (
        query.order_by(_priority_sql_order(), Task.position, Task.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    task = db.session.execute(query).scalar_one_or_none()
    if task is None:
        return None

    ttl = current_app.config["LEASE_DEFAULT_TTL"] if lease_ttl is None else lease_ttl
    expires = utcnow() + timedelta(seconds=ttl)

    # If we are reclaiming an abandoned (expired) task, retire its stale lease
    # first so the one-active-lease invariant holds.
    close_active_lease(task.id, LeaseState.expired)

    task.status = TaskStatus.in_progress
    task.owner = agent
    task.lease_expires_at = expires
    task.version += 1

    lease = Lease(task_id=task.id, agent=agent, state=LeaseState.active,
                  expires_at=expires)
    db.session.add(lease)
    log_event(project_id, "claimed", agent=agent, task_id=task.id,
              message=f"{agent} claimed {task.display_id}")
    db.session.flush()
    return task


def import_spec(project_id: int, parsed: ParsedSpec) -> dict:
    """Idempotently upsert a parsed SPEC.md tree into the project. Tasks are
    keyed by ``(project_id, key)`` so re-importing the same file is a no-op.

    Batched for full-sized backlogs (PORT-6): existing epics/tasks are loaded in
    two bulk queries (not one round-trip per task), and only genuinely-changed
    rows are written, so a ~1,500-task import is a handful of statements instead
    of ~3,000 round-trips. Per-task validation errors are collected in ``failed``
    (the row is skipped) rather than aborting the whole import."""
    created_epics = updated_epics = 0
    created_tasks = updated_tasks = unchanged_tasks = 0
    failed: list[dict] = []

    # --- epics: bulk-load existing, upsert in memory -----------------------
    existing_epics = {
        e.key: e for e in db.session.execute(
            sa.select(Epic).where(Epic.project_id == project_id)
        ).scalars().all()
    }
    epics_by_key: dict[str, Epic] = {}
    for ekey, pe in parsed.epics.items():
        epic = existing_epics.get(ekey)
        if epic is None:
            epic = Epic(project_id=project_id, key=ekey, title=pe.title,
                        section=pe.section, position=pe.position)
            db.session.add(epic)
            created_epics += 1
        else:
            epic.title = pe.title
            epic.section = pe.section
            epic.position = pe.position
            updated_epics += 1
        epics_by_key[ekey] = epic
    db.session.flush()  # assign epic ids for the FK below (one round-trip)

    # --- tasks: bulk-load existing, upsert in memory -----------------------
    existing_tasks = {
        t.key: t for t in db.session.execute(
            sa.select(Task).where(Task.project_id == project_id)
        ).scalars().all()
        if t.key is not None
    }
    # De-duplicate within the same import by key (last occurrence wins), mirroring
    # the old read-then-write path where a later duplicate updated the earlier row.
    deduped: dict[str, object] = {}
    for pt in parsed.tasks:
        try:
            validate_parsed_task(pt)
        except ValueError as exc:
            failed.append({"task_key_or_line": pt.key or pt.title or "<unknown>",
                           "error": str(exc)})
            continue
        deduped[pt.key] = pt

    for key, pt in deduped.items():
        epic_id = epics_by_key[pt.epic_key].id if pt.epic_key in epics_by_key else None
        status = TaskStatus(pt.status)
        priority = Priority(pt.priority) if pt.priority else None
        task = existing_tasks.get(key)
        if task is None:
            db.session.add(Task(
                project_id=project_id, key=pt.key, title=pt.title,
                description=pt.description, status=status, priority=priority,
                component=pt.component, proof_cmd=pt.proof_cmd,
                section=pt.section, position=pt.position, epic_id=epic_id,
            ))
            created_tasks += 1
        elif _task_unchanged(task, pt, status, priority, epic_id):
            unchanged_tasks += 1  # no write, no version bump
        else:
            task.title = pt.title
            task.description = pt.description
            task.status = status
            task.priority = priority
            task.component = pt.component
            task.proof_cmd = pt.proof_cmd
            task.section = pt.section
            task.position = pt.position
            task.epic_id = epic_id
            task.version += 1
            updated_tasks += 1
    db.session.flush()
    return {
        "epics_created": created_epics, "epics_updated": updated_epics,
        "tasks_created": created_tasks, "tasks_updated": updated_tasks,
        "tasks_unchanged": unchanged_tasks, "failed": failed,
    }


def _task_unchanged(task, pt, status, priority, epic_id) -> bool:
    """True when every import-controlled field already matches (so re-importing an
    unchanged SPEC.md is a genuine no-op — no write, no version bump)."""
    return (
        task.title == pt.title
        and task.description == pt.description
        and task.status == status
        and task.priority == priority
        and task.component == pt.component
        and task.proof_cmd == pt.proof_cmd
        and task.section == pt.section
        and task.position == pt.position
        and task.epic_id == epic_id
    )


def close_active_lease(task_id: int, state: LeaseState) -> None:
    """Mark the task's active lease as resolved (completed/released)."""
    lease = db.session.execute(
        sa.select(Lease)
        .where(Lease.task_id == task_id, Lease.state == LeaseState.active)
        .with_for_update()
    ).scalar_one_or_none()
    if lease is not None:
        lease.state = state
        lease.released_at = utcnow()
