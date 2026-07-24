"""Shared change-log helpers (UI-DELTA-3/4).

The per-project change-log records every UI-relevant mutation as one entry so the
dashboard can fetch deltas instead of refetching the whole backlog each poll. Both
storage adapters build entries the SAME way from the SAME frozen DTOs, so the feed
shape is identical on Postgres and DynamoDB (backend parity, hard rule).

* ``CHANGELOG_NAMESPACE`` — the atomic-counter namespace whose monotonic value is
  the ``seq`` cursor (allocated by the ``reserve_number`` primitive, never
  read-max-plus-one), so the cursor is a plain per-project integer on both backends.
* ``task_snapshot`` / ``epic_snapshot`` — the ``op=upsert`` payload: the entity's
  current DTO reduced to JSON-friendly scalars. For tasks it is a LEAN snapshot
  (§6.9): the scalar ``TaskOut`` fields + ``tags``, OMITTING the nested
  ``notes[]``/``commits[]`` to bound feed size. ``op=delete`` carries no snapshot.
"""
from __future__ import annotations

from datetime import datetime

# The atomic-counter namespace backing the change-log cursor. Shared by both
# adapters so the seq is the same per-project integer on each backend.
CHANGELOG_NAMESPACE = "changelog"

# Vocabulary the ``changes.entity_type`` / ``op`` columns accept. The write path
# in this task emits ``task``/``epic`` (commits, relations and notes ride an
# upsert of their parent task/epic — see the adapters); the wider set is reserved
# for the delta endpoint (UI-DELTA-5) and future first-class entries.
ENTITY_TYPES = frozenset({"task", "epic", "note", "commit", "relation"})
OPS = frozenset({"upsert", "delete"})


def _iso(v):
    """Datetime -> ISO-8601 string; pass through ``None`` and already-ISO strings so
    a snapshot round-trips identically through Postgres JSONB and a DynamoDB map."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _enum(v):
    """Enum -> its ``.value`` (both adapters build enum-typed DTO fields)."""
    return getattr(v, "value", v)


def task_snapshot(dto) -> dict:
    """Lean upsert snapshot of a task (§6.9): scalar ``TaskOut`` fields + ``tags``,
    WITHOUT the nested ``notes[]``/``commits[]`` (those bloat a chatty task's feed;
    the detail view refetches them). JSON-friendly values only."""
    return {
        "public_id": dto.public_id,
        "display_id": dto.display_id,
        "key": dto.key,
        "epic_key": dto.epic_key,
        "title": dto.title,
        "description": dto.description,
        "status": _enum(dto.status),
        "priority": _enum(dto.priority) if dto.priority is not None else None,
        "component": dto.component,
        "proof_cmd": dto.proof_cmd,
        "status_note": dto.status_note,
        "section": dto.section,
        "owner": dto.owner,
        "lease_expires_at": _iso(dto.lease_expires_at),
        "position": dto.position,
        "version": dto.version,
        "tags": list(dto.tags),
        "created_at": _iso(dto.created_at),
        "updated_at": _iso(dto.updated_at),
        "completed_at": _iso(dto.completed_at),
    }


def epic_snapshot(dto) -> dict:
    """Upsert snapshot of an epic (the full ``EpicOut`` scalar set)."""
    return {
        "public_id": dto.public_id,
        "key": dto.key,
        "title": dto.title,
        "description": dto.description,
        "section": dto.section,
        "position": dto.position,
    }
