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
ROLE_VALUES = ["reader", "writer", "admin"]  # project-membership roles (ISO-1)


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
    project = fields.Str(allow_none=True, dump_only=True)  # owning project's slug
    slug = fields.Str()
    display_name = fields.Str(allow_none=True)
    kind = fields.Str()
    created_at = fields.DateTime(dump_only=True)


# --------------------------------------------------------------------------- #
# Project membership (ISO-1) — dormant: the data model + validation only. No
# route wires these up and nothing enforces authorization from them yet.
# --------------------------------------------------------------------------- #
class MemberIn(Schema):
    principal_sub = fields.Str(
        required=True,
        metadata={"description": (
            "The principal's immutable Cognito 'sub'. Server-verified identity — "
            "callers pass the sub proven by the auth layer, never a client value."
        )},
    )
    principal_name = fields.Str(
        allow_none=True,
        metadata={"description": "Display label only (e.g. agent/user name); informational, not an identity."},
    )
    role = fields.Str(required=True, validate=validate.OneOf(ROLE_VALUES))


class MemberOut(Schema):
    project_slug = fields.Str(dump_only=True)
    principal_sub = fields.Str()
    principal_name = fields.Str(allow_none=True)
    role = fields.Str()
    created_at = fields.DateTime(dump_only=True)


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
    """A note in the project-wide feed: tagged with its scope and source key
    (`task` or `epic`, whichever it belongs to)."""
    scope = fields.Str()  # "task" | "epic"
    task = fields.Str(allow_none=True)
    epic = fields.Str(allow_none=True)
    author = fields.Str(allow_none=True)
    body = fields.Str()
    created_at = fields.DateTime(dump_only=True)


class NoteQuery(Schema):
    scope = fields.Str(
        load_default="all",
        validate=validate.OneOf(["task", "epic", "all"]),
        metadata={"description": "Which notes to include (default all)."},
    )
    author = fields.Str(metadata={"description": "Filter to one author/agent."})
    task = fields.Str(metadata={"description": "Filter to one task (key or public_id)."})
    epic = fields.Str(metadata={"description": "Filter to one epic (key)."})
    since = fields.DateTime(metadata={"description": "Only notes at/after this time (ISO 8601)."})
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class TaskOut(Schema):
    public_id = fields.Str(dump_only=True)
    display_id = fields.Str(dump_only=True)
    key = fields.Str(allow_none=True)
    epic_key = fields.Str(allow_none=True, dump_only=True)
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
    tags = fields.List(fields.Str(), dump_only=True)
    commits = fields.List(fields.Nested(CommitRefOut), dump_only=True)
    notes = fields.List(fields.Nested(NoteOut), dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
    completed_at = fields.DateTime(allow_none=True, dump_only=True)


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


class ChainRunQuery(Schema):
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


# --------------------------------------------------------------------------- #
# Invites (HA-2) — invite-only human signup. NOTE: the plaintext code is
# returned ONCE by the mint endpoint and NEVER stored or listed; the table (and
# every read schema below) only ever carries the SHA-256 `code_hash`.
# --------------------------------------------------------------------------- #
class InviteIn(Schema):
    email = fields.Email(
        allow_none=True,
        load_default=None,
        metadata={"description": (
            "Optional address to PIN the invite to (email-bound): only this "
            "address can redeem it. Omit for an open, admin-reviewed invite. The "
            "address itself is never stored — only its SHA-256 hash (email_binding)."
        )},
    )
    ttl_days = fields.Int(
        allow_none=True,
        load_default=None,
        validate=validate.Range(min=1, max=90),
        metadata={"description": "Override the invite validity window in days (default INVITE_TTL_DAYS)."},
    )
    approved = fields.Bool(
        load_default=False,
        metadata={"description": (
            "If true, mark the invite pre-approved so a future PostConfirmation "
            "hook MAY add the invitee straight to spec-readers. Default false => "
            "the invitee lands pending until an admin grants a group."
        )},
    )


class InviteMintOut(Schema):
    """The mint response — the ONLY place the plaintext code is ever emitted."""
    code = fields.Str(dump_only=True, metadata={"description": "The plaintext single-use code. Shown ONCE; never stored or logged in plaintext."})
    join_url = fields.Str(dump_only=True, metadata={"description": "The signup link carrying the code (?code=...)."})
    code_hash = fields.Str(dump_only=True, metadata={"description": "SHA-256 of the code — what is actually stored."})
    expires_at = fields.Int(dump_only=True, metadata={"description": "Epoch seconds when the invite expires (TTL)."})
    email_bound = fields.Bool(dump_only=True, metadata={"description": "Whether the invite is pinned to one address."})
    approved = fields.Bool(dump_only=True)


class InviteOut(Schema):
    """A listed invite — hashes/status/expiry only, NEVER the plaintext code."""
    code_hash = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    created_at = fields.Int(dump_only=True)
    expires_at = fields.Int(dump_only=True)
    email_bound = fields.Bool(dump_only=True)
    approved = fields.Bool(dump_only=True)


# --------------------------------------------------------------------------- #
# Agent enrollment tokens (ONBOARD-2) — single-use tokens an operator mints so a
# new AGENT can self-enroll (ONBOARD-3 redeems -> creates the Cognito user). Like
# invites: the plaintext token is returned ONCE by mint and NEVER stored/listed;
# the table (and every read schema below) only ever carries the SHA-256 token_hash.
# --------------------------------------------------------------------------- #
class EnrollmentIn(Schema):
    project_slug = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=100),
        metadata={"description": "Project the enrolled agent will be scoped to."},
    )
    agent_name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=100),
        metadata={"description": "Name of the agent being enrolled (the future Cognito user)."},
    )
    role = fields.Str(
        required=True,
        validate=validate.OneOf(ROLE_VALUES),
        metadata={"description": "Role to grant on redemption: one of reader/writer/admin."},
    )
    project_name = fields.Str(
        allow_none=True,
        load_default=None,
        validate=validate.Length(min=1, max=200),
        metadata={"description": (
            "Display name to use IF this call creates the project (project_slug is "
            "new). Ignored when the project already exists. Omitted -> a name is "
            "derived from the slug (dashes/underscores -> spaces, title-cased)."
        )},
    )
    ttl_seconds = fields.Int(
        allow_none=True,
        load_default=None,
        validate=validate.Range(min=60, max=604800),
        metadata={"description": "Override the token validity window in seconds (default ENROLL_TTL_SECONDS)."},
    )


class EnrollmentMintOut(Schema):
    """The mint response — the ONLY place the plaintext token is ever emitted."""
    enrollment_url = fields.Str(dump_only=True, metadata={"description": "The self-enroll link carrying the token in its fragment (#token=...)."})
    token = fields.Str(dump_only=True, metadata={"description": "The plaintext single-use token. Shown ONCE; never stored or logged."})
    project_slug = fields.Str(dump_only=True)
    role = fields.Str(dump_only=True)
    agent_name = fields.Str(dump_only=True)
    expires_at = fields.Int(dump_only=True, metadata={"description": "Epoch seconds when the token expires (TTL)."})
    project_created = fields.Bool(
        dump_only=True,
        metadata={"description": (
            "True iff THIS call created the project (project_slug was new). False "
            "when the project already existed (or a concurrent request created it "
            "first). Either way the enrollment token was minted."
        )},
    )


class EnrollmentOut(Schema):
    """A listed enrollment — metadata + the SHA-256 token_hash (the revocation id).

    The ``token_hash`` is a one-way hash, NOT the token: it cannot be redeemed and
    is the handle ``DELETE /agent-enrollments/<token_hash>`` keys off, so an
    admin-only list must surface it to enable revocation. The plaintext token is
    STILL never listed — it is emitted once by mint (EnrollmentMintOut) only."""
    token_hash = fields.Str(
        dump_only=True,
        metadata={"description": (
            "SHA-256 hash of the token — the revocation id (DELETE key), not a "
            "secret. The plaintext token cannot be recovered from it."
        )},
    )
    project_slug = fields.Str(dump_only=True)
    agent_name = fields.Str(dump_only=True)
    role = fields.Str(dump_only=True)
    created_by = fields.Str(dump_only=True, allow_none=True)
    created_at = fields.Int(dump_only=True, allow_none=True)
    expires_at = fields.Int(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)


class EnrollmentsQuery(Schema):
    project_slug = fields.Str(
        required=False,
        validate=validate.Length(min=1, max=100),
        metadata={"description": "Scope the listing to one project (project-admin scoped when set)."},
    )


# --------------------------------------------------------------------------- #
# Agent-enrollment REDEEM (ONBOARD-3) — the PUBLIC single-use redeem endpoint.
# A new agent posts the plaintext token it was handed; the server atomically
# burns it (single-use) and provisions a real Cognito credential, returning the
# working credentials + a copy-paste setup recipe EXACTLY ONCE.
# --------------------------------------------------------------------------- #
class EnrollRedeemIn(Schema):
    token = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=512),
        metadata={"description": "The single-use enrollment token handed to the agent. Never stored or logged."},
    )


class EnrollRedeemOut(Schema):
    """The redeem response — emits the generated password EXACTLY ONCE. The
    password is never stored or logged; a lost password means minting a fresh
    enrollment token (tokens are cheap)."""
    username = fields.Str(dump_only=True, metadata={"description": "The Cognito sign-in alias (email-as-username) for the new agent."})
    password = fields.Str(dump_only=True, metadata={"description": "The generated permanent password. Shown ONCE; never stored or logged."})
    api_base = fields.Str(dump_only=True, metadata={"description": "Base URL of the Spec Server API the agent should call."})
    region = fields.Str(dump_only=True, allow_none=True, metadata={"description": "AWS region of the Cognito pool (for InitiateAuth)."})
    client_id = fields.Str(dump_only=True, allow_none=True, metadata={"description": "Cognito app-client id used to mint tokens (USER_PASSWORD_AUTH)."})
    project_slug = fields.Str(dump_only=True, metadata={"description": "Project the agent was granted membership on."})
    role = fields.Str(dump_only=True, metadata={"description": "Role granted on the project (reader/writer/admin)."})
    recipe = fields.Dict(dump_only=True, metadata={"description": "A short copy-paste setup guide: mint a token, make the first authenticated call, and migrate a local backlog into the cloud project."})


# --------------------------------------------------------------------------- #
# Admin user lifecycle (HA-5) — approve/reject/block/delete/promote the Cognito
# users (humans AND agents) backing the pool. Approval is by GROUP membership:
# a pending human is in NO spec-* group; approve adds spec-readers/spec-writers,
# promote adds spec-admins, reject/block disables + strips spec-* groups.
# --------------------------------------------------------------------------- #
class AdminUsersQuery(Schema):
    status = fields.Str(
        required=False,
        validate=validate.OneOf(["pending", "active"]),
        metadata={"description": (
            "Filter by derived lifecycle status: 'pending' (in no spec-* group) "
            "or 'active' (in at least one spec-* group)."
        )},
    )


class AdminUserOut(Schema):
    """A listed pool user. Carries NO tokens/passwords — identity + status only."""
    username = fields.Str(dump_only=True)
    email = fields.Str(dump_only=True, allow_none=True)
    enabled = fields.Bool(dump_only=True, metadata={"description": "False once blocked/rejected (Cognito-disabled)."})
    status = fields.Str(dump_only=True, metadata={"description": "pending (no spec-* group) or active."})
    groups = fields.List(fields.Str(), dump_only=True, metadata={"description": "The user's Cognito group memberships."})
    created_at = fields.Str(dump_only=True, allow_none=True, metadata={"description": "ISO-8601 account creation time."})


class AdminApproveIn(Schema):
    group = fields.Str(
        load_default=None,
        validate=validate.OneOf(["spec-readers", "spec-writers"]),
        metadata={"description": (
            "Group to grant on approval: 'spec-readers' (default) or "
            "'spec-writers'. Admin promotion is a separate /promote action."
        )},
    )


# --------------------------------------------------------------------------- #
# Public request->approve signup queue (HA-7, bird Path A).
#
# POST /api/v1/signup is the uniform-202 anti-enumeration intake: it does ZERO
# existence work and ALWAYS returns the identical accepted body, so it is never
# an enumeration oracle. GET /api/v1/validate redeems the single-use magic link.
# The admin signups endpoints list/approve/reject requests (approve only from
# email-validated). The plaintext email is stored ONLY as an SSE-KMS attribute
# value (never a key/GSI); logs + keys carry the email_hash.
# --------------------------------------------------------------------------- #
class SignupIn(Schema):
    email = fields.Email(
        required=True,
        metadata={"description": "The email requesting access. Existence is NEVER checked on this path."},
    )
    display_name = fields.Str(
        allow_none=True, load_default=None,
        validate=validate.Length(max=64),
        metadata={"description": "Optional display name for the request (<=64 chars)."},
    )
    turnstile_token = fields.Str(
        load_default="",
        validate=validate.Length(max=4096),
        metadata={"description": "Cloudflare Turnstile token; verified server-side only when TURNSTILE_SECRET is configured."},
    )
    hp_website = fields.Str(
        load_default="",
        metadata={"description": "Honeypot — must stay empty. A non-empty value is silently dropped as a bot."},
    )


class SignupAcceptedOut(Schema):
    """The ONE fixed body every processable/dropped intake returns (no oracle)."""
    message = fields.Str(dump_only=True)


class ValidateQuery(Schema):
    token = fields.Str(
        required=True,
        metadata={"description": "The single-use magic-link token (token_id.secret) from the email."},
    )


class ValidateOut(Schema):
    outcome = fields.Str(
        dump_only=True,
        metadata={"description": "'confirmed' (valid/idempotent re-click) or 'invalid' (missing/wrong/expired/used) — never distinguished."},
    )


class AdminSignupsQuery(Schema):
    status = fields.Str(
        required=False,
        validate=validate.OneOf([
            "requested", "email-validated", "admin-approved",
            "provisioned", "rejected", "expired",
        ]),
        metadata={"description": "Filter to one signup state. Omit to list every state (newest first)."},
    )
    limit = fields.Int(
        load_default=200, validate=validate.Range(min=1, max=1000),
        metadata={"description": "Max rows to return PER state queried (newest first)."},
    )


class AdminSignupOut(Schema):
    """An admin view of a signup request. Admins may see the plaintext email (an
    SSE-KMS attribute value) to decide; logs/keys stay hashed-only."""
    email_hash = fields.Str(dump_only=True)
    email = fields.Str(dump_only=True, allow_none=True)
    display_name = fields.Str(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)
    created_at = fields.Int(dump_only=True, allow_none=True)
    updated_at = fields.Int(dump_only=True, allow_none=True)
    validated_at = fields.Int(dump_only=True, allow_none=True)
    approved_at = fields.Int(dump_only=True, allow_none=True)
    approved_by = fields.Str(dump_only=True, allow_none=True)
    rejected_by = fields.Str(dump_only=True, allow_none=True)
    reject_reason = fields.Str(dump_only=True, allow_none=True)
    provisioned_at = fields.Int(dump_only=True, allow_none=True)
    resend_count = fields.Int(dump_only=True, allow_none=True)


class AdminRejectIn(Schema):
    reason = fields.Str(
        load_default="", allow_none=True,
        metadata={"description": "Optional free-text reason recorded on the rejected row."},
    )
