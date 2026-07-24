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
    jira_issue_key: str | None = None
    jira_sync_error: str | None = None
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
    # ``task_pubid`` is the related task's stable, cross-backend ``public_id``
    # (a uuid string), or ``None`` for events that reference no task. This is the
    # single task pointer exposed by ``EventOut`` and it is populated IDENTICALLY
    # on both backends (Postgres derives it from ``Event.task.public_id``; DynamoDB
    # surfaces the ``task_pubid`` stored on the event item). The old integer
    # ``task_id`` (a Postgres-only surrogate that was always ``None`` on DynamoDB)
    # is deliberately not carried into the response — it was never a stable
    # cross-backend pointer.
    task_pubid: str | None
    message: str | None
    payload: dict
    created_at: datetime


@dataclass(frozen=True)
class ChangeDTO:
    """One entry in a project's change-log (UI-DELTA). ``seq`` is the per-project
    monotonic cursor; ``entity_pubid`` is the stable cross-backend ``public_id`` of
    the changed entity; ``snapshot`` is the entity's current lean DTO for
    ``op=upsert`` and ``None`` for ``op=delete`` (a tombstone). Built identically by
    both adapters, so the delta feed is byte-for-byte parity across backends."""
    seq: int
    entity_type: str
    entity_pubid: str
    op: str                       # "upsert" | "delete"
    version: int | None
    occurred_at: datetime
    snapshot: dict | None


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
class JiraConfigDTO:
    """Per-project Jira integration config (SLS-J3).

    Carries ONLY the ENCRYPTED token ciphertext (``api_token_encrypted``) — it
    must NEVER hold the plaintext token. The crypto boundary (``encrypt``/
    ``decrypt``) stays in the ``jira_config`` blueprint, so storage only ever
    persists/returns ciphertext and the plaintext never enters the storage layer.
    ``has_token`` is derived downstream from ``api_token_encrypted is not None``;
    the raw ciphertext is never dumped to API responses (see ``_config_to_out``).
    """
    base_url: str
    email: str
    api_token_encrypted: str | None
    jira_project_key: str
    enabled: bool
    cached_transitions: dict | None
    updated_at: datetime


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
