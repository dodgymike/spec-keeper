"""SQLAlchemy models for the Spec Server (MVP).

Isolation model (per product decision): a single shared backlog per project.
Each task carries an ``owner`` (the agent slug currently assigned/holding the
lease) so an agent can keep its specs separate simply by filtering on owner.
Workspaces/lanes, event log, decisions and chain-tracking are deferred to the
project's own SPEC.md backlog (phase 2+).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

Base = db.Model  # Flask-SQLAlchemy bound declarative base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class TaskStatus(str, enum.Enum):
    todo = "todo"            # checkbox [ ]
    in_progress = "in_progress"  # checkbox [~]
    blocked = "blocked"     # checkbox [ ] (+ note)
    deferred = "deferred"   # checkbox [ ] (+ note)
    done = "done"           # checkbox [x]
    superseded = "superseded"  # checkbox [-]
    cancelled = "cancelled"    # checkbox [-]


class Priority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class RelationKind(str, enum.Enum):
    blocks = "blocks"
    supersedes = "supersedes"
    relates = "relates"
    follow_up = "follow_up"


class LeaseState(str, enum.Enum):
    active = "active"
    released = "released"
    expired = "expired"
    completed = "completed"


# Open statuses that are eligible to be claimed by claim-next.
CLAIMABLE_STATUSES = (TaskStatus.todo,)


# --------------------------------------------------------------------------- #
# Core tables
# --------------------------------------------------------------------------- #
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    slug: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    default_branch: Mapped[str] = mapped_column(sa.Text, default="main", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )

    epics: Mapped[list["Epic"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Agent(Base):
    """Per-project registry of actors (agents/humans). Scoped to a project so
    each project has its own roster (two projects can both have a `spec-keeper`).
    Ownership of a task is still by slug string, so ad-hoc agents work without
    pre-registration; this table is metadata + the project association."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_agent_project_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(sa.Text, default="agent", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship()


class Epic(Base):
    __tablename__ = "epics"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_epic_project_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(sa.Text, nullable=False)  # e.g. RULEPERF, P0
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    section: Mapped[str] = mapped_column(sa.Text, default="backlog", nullable=False)
    position: Mapped[float] = mapped_column(sa.Float, default=1000.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="epics")
    tasks: Mapped[list["Task"]] = relationship(back_populates="epic")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Human key (e.g. "RULEPERF-9c") is unique per project when present.
        Index(
            "uq_task_project_key",
            "project_id",
            "key",
            unique=True,
            postgresql_where=sa.text("key IS NOT NULL"),
        ),
        Index(
            "ix_tasks_claim",
            "project_id",
            "status",
            "priority",
            "position",
        ),
        Index("ix_tasks_owner", "project_id", "owner"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    epic_id: Mapped[int | None] = mapped_column(
        ForeignKey("epics.id", ondelete="SET NULL")
    )
    key: Mapped[str | None] = mapped_column(sa.Text)  # full human id, e.g. P0-1
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[TaskStatus] = mapped_column(
        sa.Enum(TaskStatus, name="task_status"),
        default=TaskStatus.todo,
        nullable=False,
    )
    priority: Mapped[Priority | None] = mapped_column(
        sa.Enum(Priority, name="priority")
    )
    component: Mapped[str | None] = mapped_column(sa.Text)  # FE/BE/ML/AWS/...
    proof_cmd: Mapped[str | None] = mapped_column(sa.Text)
    status_note: Mapped[str | None] = mapped_column(sa.Text)  # blocked/deferred reason
    section: Mapped[str] = mapped_column(sa.Text, default="backlog", nullable=False)

    owner: Mapped[str | None] = mapped_column(sa.Text)  # agent slug holding it
    lease_expires_at: Mapped[datetime | None] = mapped_column()

    position: Mapped[float] = mapped_column(sa.Float, default=1000.0, nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)  # optimistic lock

    superseded_by_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column()

    project: Mapped[Project] = relationship(back_populates="tasks")
    epic: Mapped[Epic | None] = relationship(back_populates="tasks")
    tags: Mapped[list["Tag"]] = relationship(
        secondary="task_tags", back_populates="tasks"
    )
    commits: Mapped[list["CommitRef"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    notes: Mapped[list["TaskNote"]] = relationship(
        back_populates="task", cascade="all, delete-orphan",
        order_by="TaskNote.created_at",
    )

    @property
    def display_id(self) -> str:
        return self.key or self.public_id


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_tag_project_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(sa.Text, nullable=False)

    tasks: Mapped[list[Task]] = relationship(
        secondary="task_tags", back_populates="tags"
    )


class TaskTag(Base):
    __tablename__ = "task_tags"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class TaskRelation(Base):
    __tablename__ = "task_relations"
    __table_args__ = (
        UniqueConstraint("src_task_id", "dst_task_id", "kind", name="uq_relation"),
        CheckConstraint("src_task_id <> dst_task_id", name="ck_relation_self"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    src_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dst_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[RelationKind] = mapped_column(
        sa.Enum(RelationKind, name="relation_kind"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class CommitRef(Base):
    __tablename__ = "commit_refs"
    __table_args__ = (
        UniqueConstraint("task_id", "sha", name="uq_commit_task_sha"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sha: Mapped[str] = mapped_column(sa.Text, nullable=False)
    repo: Mapped[str | None] = mapped_column(sa.Text)
    test_summary: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="commits")


class TaskNote(Base):
    """A timestamped free-text note (comment) on a task. Append-only history."""

    __tablename__ = "task_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author: Mapped[str | None] = mapped_column(sa.Text)  # agent slug
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="notes")


# --------------------------------------------------------------------------- #
# Atomic number reservation (collision-proof migration/table/queue numbers)
# --------------------------------------------------------------------------- #
class Counter(Base):
    """Per-(project, namespace) monotonic counter. The composite PK serialises
    concurrent ``INSERT ... ON CONFLICT DO UPDATE`` upserts at the row level so
    two agents can never be handed the same number."""

    __tablename__ = "counters"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    namespace: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    current_value: Mapped[int] = mapped_column(default=0, nullable=False)


class Reservation(Base):
    """Audit record of an allocated number. The UNIQUE constraint is a
    belt-and-braces backstop: a duplicate value is physically unstorable."""

    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "namespace", "value", name="uq_reservation_value"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value: Mapped[int] = mapped_column(nullable=False)
    reserved_by: Mapped[str | None] = mapped_column(sa.Text)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Lease history (backs atomic claim-next; one active lease per task)
# --------------------------------------------------------------------------- #
class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index(
            "uq_one_active_lease",
            "task_id",
            unique=True,
            postgresql_where=sa.text("state = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[LeaseState] = mapped_column(
        sa.Enum(LeaseState, name="lease_state"),
        default=LeaseState.active,
        nullable=False,
    )
    acquired_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    released_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------------------- #
# Append-only log (replaces AGENT_LOG.md) and decision records (DECISIONS.md)
# --------------------------------------------------------------------------- #
class Event(Base):
    """Append-only event stream. The API never exposes update/delete; history is
    immutable. Auto-emitted on claim/complete/reserve, plus free-form notes."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_project_time", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    agent: Mapped[str | None] = mapped_column(sa.Text)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Decision(Base):
    """ADR-style decision record (replaces DECISIONS.md)."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    key: Mapped[str | None] = mapped_column(sa.Text)  # e.g. DEC-1
    agent: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    context: Mapped[str | None] = mapped_column(sa.Text)
    decision: Mapped[str] = mapped_column(sa.Text, nullable=False)
    consequences: Mapped[str | None] = mapped_column(sa.Text)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Chain runs and steps (LOG-3): track a task's mandated agent chain execution.
# --------------------------------------------------------------------------- #
class ChainRun(Base):
    """A single execution of the mandated agent chain for one task."""

    __tablename__ = "chain_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), default=_uuid, unique=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_by: Mapped[str | None] = mapped_column(sa.Text)
    # values: running / passed / failed / aborted
    status: Mapped[str] = mapped_column(sa.Text, default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column()

    steps: Mapped[list["ChainStep"]] = relationship(
        cascade="all, delete-orphan",
        order_by="ChainStep.step_order",
    )


class ChainStep(Base):
    """One step (agent) within a chain run."""

    __tablename__ = "chain_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_name", name="uq_chainstep_run_name"),
        CheckConstraint(
            "status <> 'skipped' OR skip_justification IS NOT NULL",
            name="ck_chainstep_skip_justified",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("chain_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    step_order: Mapped[int] = mapped_column(default=0, nullable=False)
    agent: Mapped[str | None] = mapped_column(sa.Text)
    # values: pending / running / passed / failed / skipped
    status: Mapped[str] = mapped_column(sa.Text, default="pending", nullable=False)
    skip_justification: Mapped[str | None] = mapped_column(sa.Text)
    output_ref: Mapped[str | None] = mapped_column(sa.Text)
