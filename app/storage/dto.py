"""Backend-neutral Data Transfer Objects (SLS-2.1).

The storage layer returns these frozen dataclasses instead of live SQLAlchemy
ORM objects, decoupling the HTTP/serialization layer from any one backend. Both
the reference PostgreSQL adapter and the DynamoDB adapter (`dynamo.py`) build these.

Attribute names are chosen to match the source fields the Marshmallow ``*Out``
schemas dump, so ``SomeOut().dump(dto)`` produces byte-for-byte the same JSON the
API returned when it dumped an ORM object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Priority, TaskStatus


@dataclass(frozen=True)
class ProjectDTO:
    public_id: str
    slug: str
    name: str
    description: str | None
    default_branch: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AgentDTO:
    public_id: str
    project: str | None          # the owning project's slug
    slug: str
    display_name: str | None
    kind: str
    created_at: datetime


@dataclass(frozen=True)
class EpicDTO:
    public_id: str
    key: str
    title: str
    description: str | None
    section: str
    position: float


@dataclass(frozen=True)
class CommitRefDTO:
    sha: str
    repo: str | None
    test_summary: str | None
    created_at: datetime


@dataclass(frozen=True)
class NoteDTO:
    author: str | None
    body: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectNoteDTO:
    """A note in the merged project-wide feed, tagged with its scope/source."""
    scope: str                   # "task" | "epic"
    task: str | None
    epic: str | None
    author: str | None
    body: str
    created_at: datetime


@dataclass(frozen=True)
class TaskDTO:
    public_id: str
    display_id: str
    key: str | None
    epic_key: str | None
    title: str
    description: str | None
    status: TaskStatus
    priority: Priority | None
    component: str | None
    proof_cmd: str | None
    status_note: str | None
    section: str
    owner: str | None
    lease_expires_at: datetime | None
    position: float
    version: int
    tags: list[str] = field(default_factory=list)
    commits: list[CommitRefDTO] = field(default_factory=list)
    notes: list[NoteDTO] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class MemberDTO:
    """A membership of a principal (an immutable Cognito ``sub``) in a project
    (ISO-1). Dormant: nothing enforces authorization from it yet. ``role`` is one
    of ``reader``/``writer``/``admin``; ``principal_name`` is an informational
    display label only (never an identity — authorization keys off ``principal_sub``)."""
    project_slug: str
    principal_sub: str
    principal_name: str | None
    role: str
    created_at: datetime


@dataclass(frozen=True)
class ReservationDTO:
    namespace: str
    value: int
    reserved_by: str | None
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class CounterDTO:
    namespace: str
    current_value: int


@dataclass(frozen=True)
class EventDTO:
    event_type: str
    agent: str | None
    # ``task_id`` is the Postgres-internal integer surrogate key of the related
    # task (what ``EventOut.task_id`` dumps as an int). DynamoDB has no integer
    # surrogate — tasks are addressed by ``public_id``/``key`` — so a Dynamo event
    # leaves this ``None`` to keep the ``EventOut`` shape identical across backends
    # (a uuid string cannot go through the ``fields.Int`` serializer). The task
    # reference on DynamoDB events is carried on the item (``task_pubid``/
    # ``task_key``) and echoed in the event ``message``/``payload`` so the activity
    # timeline can still link an event to its task.
    task_id: int | None
    message: str | None
    payload: dict
    created_at: datetime


@dataclass(frozen=True)
class DecisionDTO:
    public_id: str
    key: str | None
    title: str
    decision: str
    context: str | None
    consequences: str | None
    agent: str | None
    created_at: datetime


@dataclass(frozen=True)
class ChainStepDTO:
    step_name: str
    step_order: int
    agent: str | None
    status: str
    skip_justification: str | None
    output_ref: str | None


@dataclass(frozen=True)
class ChainRunDTO:
    public_id: str
    status: str
    started_by: str | None
    started_at: datetime
    finished_at: datetime | None
    steps: list[ChainStepDTO] = field(default_factory=list)


@dataclass(frozen=True)
class IdempotentOutcome:
    """Result of an idempotency-guarded operation (claim-next / reserve).

    * ``replay_body`` set  -> a stored response was replayed; return it verbatim
      with ``replay_status``.
    * ``result`` set       -> a fresh DTO (TaskDTO / ReservationDTO).
    * both ``None``        -> nothing to do (e.g. claim-next found no task -> 204).
    """
    result: object | None = None
    replay_body: dict | None = None
    replay_status: int | None = None
