"""Marshmallow schemas.

These are the single source of truth for both request validation and the
auto-generated OpenAPI document that agents consume.
"""
from __future__ import annotations

from marshmallow import Schema, fields, validate

from .models import Priority, RelationKind, TaskStatus, LeaseState  # noqa: F401

STATUS_VALUES = [s.value for s in TaskStatus]
PRIORITY_VALUES = [p.value for p in Priority]
RELATION_VALUES = [r.value for r in RelationKind]


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
class ProjectIn(Schema):
    slug = fields.Str(required=True, metadata={"description": "URL-safe unique key, e.g. 'corsearch'"})
    name = fields.Str(required=True)
    description = fields.Str(allow_none=True)
    default_branch = fields.Str(load_default="main")


class ProjectOut(Schema):
    public_id = fields.Str(dump_only=True)
    slug = fields.Str()
    name = fields.Str()
    description = fields.Str(allow_none=True)
    default_branch = fields.Str()
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class ProjectPatch(Schema):
    name = fields.Str()
    description = fields.Str(allow_none=True)
    default_branch = fields.Str()


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
class AgentIn(Schema):
    slug = fields.Str(required=True)
    display_name = fields.Str(allow_none=True)
    kind = fields.Str(load_default="agent", validate=validate.OneOf(["agent", "human"]))


class AgentOut(Schema):
    public_id = fields.Str(dump_only=True)
    project = fields.Method("get_project", dump_only=True)
    slug = fields.Str()
    display_name = fields.Str(allow_none=True)
    kind = fields.Str()
    created_at = fields.DateTime(dump_only=True)

    def get_project(self, obj):
        return obj.project.slug if obj.project else None


# --------------------------------------------------------------------------- #
# Epics
# --------------------------------------------------------------------------- #
class EpicIn(Schema):
    key = fields.Str(required=True, metadata={"description": "ID prefix, e.g. 'RULEPERF'"})
    title = fields.Str(required=True)
    description = fields.Str(allow_none=True)
    section = fields.Str(
        load_default="backlog",
        validate=validate.OneOf(["backlog", "to_do", "in_progress", "completed"]),
    )
    position = fields.Float(load_default=1000.0)


class EpicOut(Schema):
    public_id = fields.Str(dump_only=True)
    key = fields.Str()
    title = fields.Str()
    description = fields.Str(allow_none=True)
    section = fields.Str()
    position = fields.Float()


class EpicPatch(Schema):
    title = fields.Str()
    description = fields.Str(allow_none=True)
    section = fields.Str(
        validate=validate.OneOf(["backlog", "to_do", "in_progress", "completed"])
    )
    position = fields.Float()


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
class CommitRefOut(Schema):
    sha = fields.Str()
    repo = fields.Str(allow_none=True)
    test_summary = fields.Str(allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class NoteIn(Schema):
    body = fields.Str(required=True, metadata={"description": "The note text."})
    author = fields.Str(allow_none=True, metadata={"description": "Agent slug who wrote it."})


class NoteOut(Schema):
    author = fields.Str(allow_none=True)
    body = fields.Str()
    created_at = fields.DateTime(dump_only=True)


class ProjectNoteOut(Schema):
    """A note with its task context, for the project-wide notes feed."""
    task = fields.Method("get_task", dump_only=True)
    author = fields.Str(allow_none=True)
    body = fields.Str()
    created_at = fields.DateTime(dump_only=True)

    def get_task(self, obj):
        return obj.task.display_id if obj.task else None


class NoteQuery(Schema):
    author = fields.Str(metadata={"description": "Filter to one author/agent."})
    task = fields.Str(metadata={"description": "Filter to one task (key or public_id)."})
    since = fields.DateTime(metadata={"description": "Only notes at/after this time (ISO 8601)."})
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class TaskOut(Schema):
    public_id = fields.Str(dump_only=True)
    display_id = fields.Str(dump_only=True)
    key = fields.Str(allow_none=True)
    epic_key = fields.Method("get_epic_key", dump_only=True)
    title = fields.Str()
    description = fields.Str(allow_none=True)
    status = fields.Enum(TaskStatus, by_value=True)
    priority = fields.Enum(Priority, by_value=True, allow_none=True)
    component = fields.Str(allow_none=True)
    proof_cmd = fields.Str(allow_none=True)
    status_note = fields.Str(allow_none=True)
    section = fields.Str()
    owner = fields.Str(allow_none=True)
    lease_expires_at = fields.DateTime(allow_none=True, dump_only=True)
    position = fields.Float()
    version = fields.Int(dump_only=True, metadata={"description": "Optimistic-lock token; send back as If-Match."})
    tags = fields.Method("get_tags", dump_only=True)
    commits = fields.List(fields.Nested(CommitRefOut), dump_only=True)
    notes = fields.List(fields.Nested(NoteOut), dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
    completed_at = fields.DateTime(allow_none=True, dump_only=True)

    def get_epic_key(self, obj):
        return obj.epic.key if obj.epic else None

    def get_tags(self, obj):
        return [t.key for t in obj.tags]


class TaskIn(Schema):
    key = fields.Str(allow_none=True, metadata={"description": "Human ID, e.g. 'P0-1'. Optional."})
    epic_key = fields.Str(allow_none=True)
    title = fields.Str(required=True)
    description = fields.Str(allow_none=True)
    status = fields.Str(load_default="todo", validate=validate.OneOf(STATUS_VALUES))
    priority = fields.Str(allow_none=True, validate=validate.OneOf(PRIORITY_VALUES))
    component = fields.Str(allow_none=True)
    proof_cmd = fields.Str(allow_none=True)
    section = fields.Str(load_default="backlog")
    position = fields.Float(load_default=1000.0)
    created_by = fields.Str(allow_none=True)
    tags = fields.List(fields.Str(), load_default=list)


class TaskPatch(Schema):
    title = fields.Str()
    description = fields.Str(allow_none=True)
    status = fields.Str(validate=validate.OneOf(STATUS_VALUES))
    status_note = fields.Str(allow_none=True)
    priority = fields.Str(allow_none=True, validate=validate.OneOf(PRIORITY_VALUES))
    component = fields.Str(allow_none=True)
    proof_cmd = fields.Str(allow_none=True)
    section = fields.Str()
    position = fields.Float()
    owner = fields.Str(allow_none=True)
    epic_key = fields.Str(allow_none=True)


class TaskQuery(Schema):
    status = fields.Str(validate=validate.OneOf(STATUS_VALUES))
    owner = fields.Str(metadata={"description": "Filter to one agent's specs."})
    epic = fields.Str(metadata={"description": "Epic key."})
    priority = fields.Str(validate=validate.OneOf(PRIORITY_VALUES))
    tag = fields.Str()
    q = fields.Str(metadata={"description": "Free-text match on title/description."})
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class ClaimNextIn(Schema):
    agent = fields.Str(required=True, metadata={"description": "Claiming agent's slug."})
    epic = fields.Str(allow_none=True, metadata={"description": "Restrict to this epic."})
    priority_max = fields.Str(
        allow_none=True,
        validate=validate.OneOf(PRIORITY_VALUES),
        metadata={"description": "Only consider tasks at or above this priority."},
    )
    component = fields.Str(allow_none=True)
    lease_ttl = fields.Int(allow_none=True, metadata={"description": "Lease seconds; defaults to server config."})


class CompleteIn(Schema):
    commit_sha = fields.Str(allow_none=True)
    repo = fields.Str(allow_none=True)
    test_summary = fields.Str(allow_none=True)
    proof_cmd = fields.Str(allow_none=True)


class StatusIn(Schema):
    status = fields.Str(required=True, validate=validate.OneOf(STATUS_VALUES))
    note = fields.Str(allow_none=True)


class ReleaseIn(Schema):
    reset_to = fields.Str(
        load_default="todo",
        validate=validate.OneOf(STATUS_VALUES),
        metadata={"description": "Status to set on release (default todo)."},
    )


class RelationIn(Schema):
    target = fields.Str(required=True, metadata={"description": "Target task key or public_id."})
    kind = fields.Str(required=True, validate=validate.OneOf(RELATION_VALUES))


class CommitIn(Schema):
    sha = fields.Str(required=True)
    repo = fields.Str(allow_none=True)
    test_summary = fields.Str(allow_none=True)


# --------------------------------------------------------------------------- #
# Reservations
# --------------------------------------------------------------------------- #
class ReservationIn(Schema):
    namespace = fields.Str(required=True, metadata={"description": "e.g. 'migration', 'table', 'queue'."})
    reserved_by = fields.Str(allow_none=True)
    task_key = fields.Str(allow_none=True)
    note = fields.Str(allow_none=True)


class ReservationOut(Schema):
    namespace = fields.Str()
    value = fields.Int()
    reserved_by = fields.Str(allow_none=True)
    note = fields.Str(allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class CounterOut(Schema):
    namespace = fields.Str()
    current_value = fields.Int()


class MessageOut(Schema):
    message = fields.Str()


# --------------------------------------------------------------------------- #
# Events (append-only log) and decisions
# --------------------------------------------------------------------------- #
class EventIn(Schema):
    event_type = fields.Str(load_default="note")
    agent = fields.Str(allow_none=True)
    task_key = fields.Str(allow_none=True)
    message = fields.Str(allow_none=True)
    payload = fields.Dict(load_default=dict)


class EventOut(Schema):
    event_type = fields.Str()
    agent = fields.Str(allow_none=True)
    task_id = fields.Int(allow_none=True)
    message = fields.Str(allow_none=True)
    payload = fields.Dict()
    created_at = fields.DateTime(dump_only=True)


class EventQuery(Schema):
    event_type = fields.Str()
    agent = fields.Str()
    task = fields.Str(metadata={"description": "Task key or public_id."})
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class DecisionIn(Schema):
    key = fields.Str(allow_none=True, metadata={"description": "e.g. DEC-7"})
    title = fields.Str(required=True)
    decision = fields.Str(required=True)
    context = fields.Str(allow_none=True)
    consequences = fields.Str(allow_none=True)
    agent = fields.Str(allow_none=True)
    task_key = fields.Str(allow_none=True)


class DecisionOut(Schema):
    public_id = fields.Str(dump_only=True)
    key = fields.Str(allow_none=True)
    title = fields.Str()
    decision = fields.Str()
    context = fields.Str(allow_none=True)
    consequences = fields.Str(allow_none=True)
    agent = fields.Str(allow_none=True)
    created_at = fields.DateTime(dump_only=True)


# --------------------------------------------------------------------------- #
# Chain runs and steps (LOG-3)
# --------------------------------------------------------------------------- #
STEP_STATUS_VALUES = ["pending", "running", "passed", "failed", "skipped"]
RUN_STATUS_VALUES = ["running", "passed", "failed", "aborted"]


class ChainRunIn(Schema):
    started_by = fields.Str(allow_none=True)


class ChainStepOut(Schema):
    step_name = fields.Str()
    step_order = fields.Int()
    agent = fields.Str(allow_none=True)
    status = fields.Str()
    skip_justification = fields.Str(allow_none=True)
    output_ref = fields.Str(allow_none=True)


class ChainRunOut(Schema):
    public_id = fields.Str(dump_only=True)
    status = fields.Str()
    started_by = fields.Str(allow_none=True)
    started_at = fields.DateTime(dump_only=True)
    finished_at = fields.DateTime(allow_none=True, dump_only=True)
    steps = fields.List(fields.Nested(ChainStepOut), dump_only=True)


class ChainStepIn(Schema):
    # Optional in the body: the endpoint fills it from the URL path when omitted.
    step_name = fields.Str(load_default=None)
    step_order = fields.Int(load_default=0)
    agent = fields.Str(allow_none=True)
    status = fields.Str(required=True, validate=validate.OneOf(STEP_STATUS_VALUES))
    skip_justification = fields.Str(allow_none=True)
    output_ref = fields.Str(allow_none=True)


class ChainRunPatch(Schema):
    status = fields.Str(validate=validate.OneOf(RUN_STATUS_VALUES))
