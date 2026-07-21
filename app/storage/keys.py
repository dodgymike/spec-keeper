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
