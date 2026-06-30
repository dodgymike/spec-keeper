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
    Lease,
    LeaseState,
    Priority,
    Reservation,
    Task,
    TaskStatus,
    utcnow,
)

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
    claimable = [s for s in CLAIMABLE_STATUSES]
    query = (
        sa.select(Task)
        .where(Task.project_id == project_id)
        .where(Task.status.in_(claimable))
        # only claim unowned tasks (or tasks whose lease we already hold)
        .where(sa.or_(Task.owner.is_(None), Task.owner == agent))
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

    ttl = lease_ttl or current_app.config["LEASE_DEFAULT_TTL"]
    expires = utcnow() + timedelta(seconds=ttl)

    task.status = TaskStatus.in_progress
    task.owner = agent
    task.lease_expires_at = expires
    task.version += 1

    lease = Lease(task_id=task.id, agent=agent, state=LeaseState.active,
                  expires_at=expires)
    db.session.add(lease)
    db.session.flush()
    return task


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
