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
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import (
    CLAIMABLE_STATUSES,
    Change,
    Epic,
    Event,
    Lease,
    LeaseState,
    Priority,
    Reservation,
    Tag,
    Task,
    TaskStatus,
    _uuid,
    utcnow,
)
from .specmd import ParsedSpec, validate_doc_task, validate_parsed_task
from .storage.changelog import CHANGELOG_NAMESPACE


def log_event(project_id: int, event_type: str, agent=None, task_id=None,
              message=None, payload=None) -> Event:
    """Append an immutable event to the project's stream."""
    event = Event(
        project_id=project_id, event_type=event_type, agent=agent,
        task_id=task_id, message=message, payload=payload or {},
    )
    db.session.add(event)
    return event

def _next_change_seq(project_id: int) -> int:
    """Allocate the next per-project change-log ``seq`` via the SAME atomic counter
    upsert that backs collision-proof reservation (namespace ``changelog``). Runs in
    the caller's transaction, so the seq bump and the change row commit/roll back
    together — never read-max-plus-one."""
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
        .bindparams(pid=project_id, ns=CHANGELOG_NAMESPACE)
    )
    return int(db.session.execute(stmt).scalar_one())


def record_change(project_id: int, entity_type: str, entity_pubid: str, op: str,
                  *, version: int | None = None, snapshot: dict | None = None) -> Change:
    """Append one change-log entry (UI-DELTA) inside the current transaction.

    The entry is NOT committed here — the caller's mutation owns the transaction, so
    the change entry and the entity write are atomic (a rollback drops both). ``seq``
    is the monotonic per-project cursor; ``snapshot`` is the entity's lean DTO for
    ``op=upsert`` and ``None`` for ``op=delete`` (a tombstone)."""
    seq = _next_change_seq(project_id)
    change = Change(
        project_id=project_id, seq=seq, entity_type=entity_type,
        entity_pubid=entity_pubid, op=op, version=version, snapshot=snapshot,
        occurred_at=utcnow(),
    )
    db.session.add(change)
    return change


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

    # --- tasks: bulk-load existing (with tags), upsert in memory -----------
    # ``selectinload(Task.tags)`` eager-loads every task's tags in ONE extra
    # query (not one lazy round-trip per task), so the tag-parity check below
    # keeps PORT-6's batched-import shape.
    existing_tasks = {
        t.key: t for t in db.session.execute(
            sa.select(Task)
            .where(Task.project_id == project_id)
            .options(selectinload(Task.tags))
        ).scalars().all()
        if t.key is not None
    }
    # Bulk-load existing tags once; ``_tag`` get-or-creates in memory so an
    # import that reuses/adds tags is still a handful of statements. Parsed tags
    # are de-duplicated (order-preserving) to match the many-to-many, which can
    # hold each (task, tag) association only once.
    existing_tags = {
        tag.key: tag for tag in db.session.execute(
            sa.select(Tag).where(Tag.project_id == project_id)
        ).scalars().all()
    }

    def _tag(key: str) -> Tag:
        tag = existing_tags.get(key)
        if tag is None:
            tag = Tag(project_id=project_id, key=key)
            db.session.add(tag)
            existing_tags[key] = tag
        return tag
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
        desired_tags = list(dict.fromkeys(pt.tags or []))
        task = existing_tasks.get(key)
        if task is None:
            db.session.add(Task(
                project_id=project_id, key=pt.key, title=pt.title,
                description=pt.description, status=status, priority=priority,
                component=pt.component, proof_cmd=pt.proof_cmd,
                section=pt.section, position=pt.position, epic_id=epic_id,
                tags=[_tag(k) for k in desired_tags],
            ))
            created_tasks += 1
        elif _task_unchanged(task, pt, status, priority, epic_id, desired_tags):
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
            task.tags = [_tag(k) for k in desired_tags]
            task.version += 1
            updated_tasks += 1
    db.session.flush()
    return {
        "epics_created": created_epics, "epics_updated": updated_epics,
        "tasks_created": created_tasks, "tasks_updated": updated_tasks,
        "tasks_unchanged": unchanged_tasks, "failed": failed,
    }


def import_doc(project_id: int, doc: dict) -> dict:
    """Idempotently upsert a full-fidelity JSON document (PORT-8) into the project.

    Unlike ``import_spec`` (which dedups tasks on their human ``key`` and so drops
    KEYLESS tasks), this dedups on the stable ``public_id`` — so every task, keyed
    AND keyless, round-trips losslessly, and re-importing an unchanged document is
    a genuine no-op (no write, no version bump). The supplied ``public_id`` is
    preserved on create so ``import(export(project))`` re-exports identically.

    Batched exactly like ``import_spec``: existing epics/tasks are bulk-loaded in
    a handful of queries (not one round-trip per task), and only genuinely-changed
    rows are written. Per-task validation errors are collected in ``failed`` (the
    row is skipped) rather than aborting the whole import.

    Runtime state (owner/lease/version) is NOT taken from the payload: a fresh
    import starts each task unowned at version 1.
    """
    created_epics = updated_epics = 0
    created_tasks = updated_tasks = unchanged_tasks = 0
    failed: list[dict] = []

    # --- epics: bulk-load existing, upsert by key (epics require a key) -----
    existing_epics = {
        e.key: e for e in db.session.execute(
            sa.select(Epic).where(Epic.project_id == project_id)
        ).scalars().all()
    }
    epics_by_key: dict[str, Epic] = dict(existing_epics)
    for pe in doc.get("epics", []) or []:
        ekey = pe.get("key")
        if not ekey:
            continue
        epic = existing_epics.get(ekey)
        if epic is None:
            # Epics dedup on (project, key), NOT public_id, so a fresh public_id
            # is minted here — preserving the payload's would collide across
            # projects (public_id is globally unique) with no idempotency benefit.
            epic = Epic(
                project_id=project_id, key=ekey, title=pe.get("title") or ekey,
                description=pe.get("description"),
                section=pe.get("section") or "backlog",
                position=pe.get("position", 1000.0) if pe.get("position") is not None else 1000.0,
            )
            db.session.add(epic)
            created_epics += 1
        else:
            epic.title = pe.get("title") or epic.title
            epic.description = pe.get("description")
            epic.section = pe.get("section") or "backlog"
            if pe.get("position") is not None:
                epic.position = pe["position"]
            updated_epics += 1
        epics_by_key[ekey] = epic
    db.session.flush()  # assign epic ids for the FK below (one round-trip)

    # --- tasks: bulk-load existing by public_id, upsert in memory -----------
    existing_tasks = {
        t.public_id: t for t in db.session.execute(
            sa.select(Task)
            .where(Task.project_id == project_id)
            .options(selectinload(Task.tags))
        ).scalars().all()
    }
    existing_tags = {
        tag.key: tag for tag in db.session.execute(
            sa.select(Tag).where(Tag.project_id == project_id)
        ).scalars().all()
    }

    def _tag(key: str) -> Tag:
        tag = existing_tags.get(key)
        if tag is None:
            tag = Tag(project_id=project_id, key=key)
            db.session.add(tag)
            existing_tags[key] = tag
        return tag

    # De-duplicate within this import by public_id (last wins), mirroring
    # ``import_spec``'s per-key dedup — so a payload that repeats a public_id
    # yields one upsert (not an IntegrityError), identically to the DynamoDB
    # adapter's last-write-wins batch. Tasks without a public_id (hand-authored
    # docs) always create, so they are processed as-is under a minted id.
    deduped: dict[str, dict] = {}
    keyless_rows: list[dict] = []
    for t in doc.get("tasks", []) or []:
        try:
            validate_doc_task(t)
        except ValueError as exc:
            failed.append({
                "task_key_or_line": t.get("key") or t.get("title")
                or t.get("public_id") or "<unknown>",
                "error": str(exc),
            })
            continue
        if t.get("public_id"):
            deduped[t["public_id"]] = t
        else:
            keyless_rows.append(t)

    for t in list(deduped.values()) + keyless_rows:
        pubid = t.get("public_id") or _uuid()
        epic_key = t.get("epic_key")
        epic = epics_by_key.get(epic_key) if epic_key else None
        epic_id = epic.id if epic is not None else None
        status = TaskStatus(t["status"]) if t.get("status") else TaskStatus.todo
        priority = Priority(t["priority"]) if t.get("priority") else None
        section = t.get("section") or "backlog"
        position = t.get("position", 1000.0) if t.get("position") is not None else 1000.0
        desired_tags = list(dict.fromkeys(t.get("tags") or []))

        task = existing_tasks.get(pubid)
        if task is None:
            db.session.add(Task(
                project_id=project_id, public_id=pubid, key=t.get("key"),
                title=t["title"], description=t.get("description"),
                status=status, priority=priority, component=t.get("component"),
                proof_cmd=t.get("proof_cmd"), status_note=t.get("status_note"),
                section=section, position=position, epic_id=epic_id,
                tags=[_tag(k) for k in desired_tags],
                created_at=t.get("created_at") or utcnow(),
                updated_at=t.get("updated_at") or utcnow(),
                completed_at=t.get("completed_at"),
            ))
            created_tasks += 1
        elif _doc_task_unchanged(task, t, status, priority, epic_id, section,
                                 position, desired_tags):
            unchanged_tasks += 1  # no write, no version bump
        else:
            task.key = t.get("key")
            task.title = t["title"]
            task.description = t.get("description")
            task.status = status
            task.priority = priority
            task.component = t.get("component")
            task.proof_cmd = t.get("proof_cmd")
            task.status_note = t.get("status_note")
            task.section = section
            task.position = position
            task.epic_id = epic_id
            task.tags = [_tag(k) for k in desired_tags]
            if t.get("completed_at") is not None:
                task.completed_at = t.get("completed_at")
            task.version += 1
            updated_tasks += 1
    db.session.flush()
    return {
        "epics_created": created_epics, "epics_updated": updated_epics,
        "tasks_created": created_tasks, "tasks_updated": updated_tasks,
        "tasks_unchanged": unchanged_tasks, "failed": failed,
    }


def _doc_task_unchanged(task, t, status, priority, epic_id, section, position,
                        desired_tags) -> bool:
    """True when every import-controlled field of the stored task already matches
    the JSON payload (so re-importing an unchanged document is a genuine no-op).
    Timestamps and runtime state are not part of change detection — identical to
    the DynamoDB adapter's ``_doc_item_unchanged`` (parity)."""
    return (
        task.key == t.get("key")
        and task.title == t.get("title")
        and task.description == t.get("description")
        and task.status == status
        and task.priority == priority
        and task.component == t.get("component")
        and task.proof_cmd == t.get("proof_cmd")
        and task.status_note == t.get("status_note")
        and task.section == section
        and task.position == position
        and task.epic_id == epic_id
        and {tag.key for tag in task.tags} == set(desired_tags)
    )


def _task_unchanged(task, pt, status, priority, epic_id, desired_tags) -> bool:
    """True when every import-controlled field already matches (so re-importing an
    unchanged SPEC.md is a genuine no-op — no write, no version bump). Tags are
    compared as a set (association order is not meaningful) so a tag-only change
    IS detected and updates the task, identically to the DynamoDB adapter."""
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
        and {t.key for t in task.tags} == set(desired_tags)
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
