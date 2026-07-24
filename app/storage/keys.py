"""DynamoDB single-table key encoders (SLS-3).

Pure functions that build the ``PK``/``SK`` and GSI key strings for every item
type in the single-table design. The layout is the source-of-truth from
``STORAGE_ABSTRACTION_DEEPDIVE.md`` §3.1 (item shapes) and §3.2 (the 5 GSIs) and
mirrors ``infra/terraform/dynamodb.tf``.

Conventions
-----------
* Every item for a project lives under partition ``P#<slug>``.
* A task's children (notes/commits/relations) share the ``TASK#<pubid>`` SK
  prefix so one ``Query begins_with`` returns the task + its children.
* ``<ts>`` is an ISO-8601 UTC timestamp (lexicographically sortable).
* Numeric sort keys (priority/position/reservation value) are zero-padded so a
  string range key sorts identically to the numeric value.

No user input is ever interpolated into a DynamoDB *expression*; these strings
only ever become item key *values* (bound via ExpressionAttributeValues or the
resource layer), so there is no injection surface — the analogue of the
parameterised SQL rule.
"""
from __future__ import annotations

# --- GSI attribute names (must match dynamodb.tf) ------------------------- #
GSI1 = "GSI1"   # claim / status
GSI2 = "GSI2"   # owner (sparse)
GSI3 = "GSI3"   # task-key (sparse)
GSI4 = "GSI4"   # feed: events / notes
GSI5 = "GSI5"   # all-projects
GSI6 = "GSI6"   # project membership: list a principal's projects (ISO-1)
GSI7 = "GSI7"   # change-log: per-project ascending seq feed (UI-DELTA)

# Feed "kind" discriminators for GSI4.
FEED_EVENT = "EVT"
FEED_TASK_NOTE = "TN"
FEED_EPIC_NOTE = "EN"

_ALL_PROJECTS = "PROJECTS"

# Priority -> rank (lower sorts first); no-priority sorts last, matching the
# Postgres ``_priority_sql_order`` (P0..P3 then NULL).
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
NO_PRIORITY_RANK = 9


# --- partition key -------------------------------------------------------- #
def pk(slug: str) -> str:
    return f"P#{slug}"


# --- sort keys ------------------------------------------------------------ #
def project_sk() -> str:
    return "META"


def agent_sk(slug: str) -> str:
    return f"AGENT#{slug}"


def epic_sk(key: str) -> str:
    return f"EPIC#{key}"


def member_sk(sub: str) -> str:
    return f"MEMBER#{sub}"


def member_prefix() -> str:
    """begins_with prefix returning a project's membership item collection."""
    return "MEMBER#"


def epic_note_sk(key: str, ts: str, uid: str) -> str:
    return f"EPIC#{key}#NOTE#{ts}#{uid}"


def task_sk(pubid: str) -> str:
    return f"TASK#{pubid}"


def task_prefix(pubid: str) -> str:
    """begins_with prefix that returns the task item + all its children."""
    return f"TASK#{pubid}"


def task_note_sk(pubid: str, ts: str, uid: str) -> str:
    return f"TASK#{pubid}#NOTE#{ts}#{uid}"


def commit_sk(pubid: str, sha: str) -> str:
    return f"TASK#{pubid}#COMMIT#{sha}"


def relation_sk(pubid: str, kind: str, dst_pubid: str) -> str:
    return f"TASK#{pubid}#REL#{kind}#{dst_pubid}"


def relation_in_sk(dst_pubid: str, kind: str, src_pubid: str) -> str:
    """Mirror SK for the INCOMING edge (D1, SLS-J2).

    The forward edge lives under the source's ``TASK#<src>#REL#<kind>#<dst>``;
    this mirror item lives under the destination's ``TASK#<dst>#RELIN#<kind>#<src>``
    so an incoming-edge query is a ``begins_with(TASK#<dst>#RELIN#)`` range read on
    the same partition — no new GSI. Written in the same TransactWriteItems as the
    forward edge, so the pair is all-or-nothing."""
    return f"TASK#{dst_pubid}#RELIN#{kind}#{src_pubid}"


def relation_out_prefix(pubid: str) -> str:
    """begins_with prefix returning a task's OUTGOING relation edges."""
    return f"TASK#{pubid}#REL#"


def relation_in_prefix(pubid: str) -> str:
    """begins_with prefix returning a task's INCOMING relation mirror edges."""
    return f"TASK#{pubid}#RELIN#"


def jira_config_sk() -> str:
    """Singleton SK for a project's Jira integration config (SLS-J3).

    Exactly one config item per project lives under ``P#<slug>`` at this fixed
    SK, mirroring the Postgres ``UNIQUE(project_id)`` on ``jira_project_config``.
    A conditional put on ``attribute_not_exists(PK)`` enforces create-once
    (Conflict), matching the Postgres uniqueness backstop."""
    return "JIRACFG"


def counter_sk(namespace: str) -> str:
    return f"COUNTER#{namespace}"


def reservation_sk(namespace: str, value: int) -> str:
    return f"RES#{namespace}#{value:020d}"


def reservation_prefix(namespace: str | None = None) -> str:
    return "RES#" if namespace is None else f"RES#{namespace}#"


def event_sk(ts: str, uid: str) -> str:
    return f"EVT#{ts}#{uid}"


def decision_sk(ts: str, uid: str) -> str:
    return f"DEC#{ts}#{uid}"


def chain_run_sk(run_pubid: str) -> str:
    return f"CRUN#{run_pubid}"


def chain_step_sk(run_pubid: str, step_name: str) -> str:
    return f"CRUN#{run_pubid}#STEP#{step_name}"


def chain_step_prefix(run_pubid: str) -> str:
    return f"CRUN#{run_pubid}#STEP#"


def chain_run_prefix() -> str:
    """begins_with prefix for a project's chain-run item collection (runs +
    their step children share the ``CRUN#`` prefix; filter by ``type``)."""
    return "CRUN#"


def idempotency_sk(endpoint: str, key: str) -> str:
    return f"IDEM#{endpoint}#{key}"


# --- GSI keys ------------------------------------------------------------- #
def gsi1_status_pk(slug: str, status: str) -> str:
    return f"P#{slug}#ST#{status}"


def gsi1_sk(priority_rank: int, position: float, pubid: str) -> str:
    # zero-padded so lexicographic order == (priority, position) numeric order.
    return f"{priority_rank}#{position:020.4f}#{pubid}"


def gsi2_owner_pk(slug: str, owner: str) -> str:
    return f"P#{slug}#OWN#{owner}"


def gsi2_sk(pubid: str) -> str:
    return f"TASK#{pubid}"


def gsi3_key_pk(slug: str, key: str) -> str:
    return f"P#{slug}#KEY#{key}"


def gsi3_sk(pubid: str) -> str:
    return f"TASK#{pubid}"


def gsi4_feed_pk(slug: str, kind: str) -> str:
    return f"P#{slug}#FEED#{kind}"


def gsi4_sk(ts: str, uid: str) -> str:
    return f"{ts}#{uid}"


def gsi5_pk() -> str:
    return _ALL_PROJECTS


def gsi5_sk(slug: str) -> str:
    return slug


# GSI6: list a principal's projects. Reserved index number 6 (namespace
# "dynamo-gsi"). PK=MEMBER#<sub> gathers every project a principal belongs to;
# SK=<slug> so a principal's projects sort by slug. Member items only.
def gsi6_member_pk(sub: str) -> str:
    return f"MEMBER#{sub}"


def gsi6_sk(slug: str) -> str:
    return slug


# --- change-log (UI-DELTA) ------------------------------------------------ #
# The change item lives on the base partition under SK CHANGE#<zero-padded seq>
# so a base-table range read is already in numeric seq order. GSI7 (reserved
# index number 7, namespace "dynamo-gsi") gives an ascending "seq > cursor"
# delta query: PK=P#<slug>#CHANGES, SK=<zero-padded seq>. Zero-padding makes the
# lexicographic string order identical to the numeric seq order.
def change_sk(seq: int) -> str:
    return f"CHANGE#{seq:020d}"


def change_prefix() -> str:
    return "CHANGE#"


def gsi7_changes_pk(slug: str) -> str:
    return f"P#{slug}#CHANGES"


def gsi7_sk(seq: int) -> str:
    return f"{seq:020d}"
