"""Idempotency-Key support (HARDEN-3).

Stores the response of a successful, non-idempotent operation keyed by the
caller-supplied ``Idempotency-Key`` header so a retried request replays the
original result instead of performing the action twice (e.g. claiming a second
task or burning a second reservation number).
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from flask import request
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .extensions import db
from .models import utcnow

Base = db.Model

#: The request header agents send to make an operation idempotent.
IDEMPOTENCY_HEADER = "Idempotency-Key"


class IdempotencyKey(Base):
    """One stored response per (project, endpoint, key) triple."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("project_id", "endpoint", "key", name="uq_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(sa.Text, nullable=False)  # "claim-next"/"reserve"
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status_code: Mapped[int] = mapped_column(default=200, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


def idempotency_key_from_request() -> str | None:
    """Return the ``Idempotency-Key`` request header value, or None if absent."""
    return request.headers.get(IDEMPOTENCY_HEADER)


def lookup_idempotent(project_id: int, endpoint: str, key: str) -> IdempotencyKey | None:
    """Return the stored row for this (project, endpoint, key), or None."""
    return db.session.execute(
        sa.select(IdempotencyKey).where(
            IdempotencyKey.project_id == project_id,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.key == key,
        )
    ).scalar_one_or_none()


def store_idempotent(
    project_id: int,
    endpoint: str,
    key: str,
    response_json: dict,
    status_code: int = 200,
) -> IdempotencyKey:
    """Persist a response for replay.

    Handles the race where a concurrent request inserted the same key first:
    on IntegrityError we roll back and return the now-existing row rather than
    raising, so both callers observe the same stored response.
    """
    row = IdempotencyKey(
        project_id=project_id,
        endpoint=endpoint,
        key=key,
        response_json=response_json,
        status_code=status_code,
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = lookup_idempotent(project_id, endpoint, key)
        if existing is not None:
            return existing
        raise
    return row
