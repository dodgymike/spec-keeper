"""DynamoDB storage adapter (SLS-3..SLS-6).

A second, config-selected ``StorageBackend`` over a single DynamoDB table (boto3
resource + client). It returns the SAME frozen DTOs and raises the SAME neutral
errors (``NotFound``/``Conflict``/``VersionConflict``/``BackendUnavailable``) as
the reference ``PostgresBackend`` — the whole point being behavioural + concurrency
parity (SLS-8). The key/GSI design is ``STORAGE_ABSTRACTION_DEEPDIVE.md`` §3 and
mirrors ``infra/terraform/dynamodb.tf``; encoders live in ``keys.py``.

Guarantee mapping (deep-dive §4):
* atomic claim   -> GSI1 candidate Query + conditional ``UpdateItem``
  (``attribute_not_exists(owner) AND status=todo``), retry next candidate on
  ConditionalCheckFailed. Two racers never both win.
* atomic reserve -> per-item atomic ``ADD current_value`` (serialised) +
  conditional-put UNIQUE(namespace,value) backstop, audit+event in one
  ``TransactWriteItems``.
* optimistic lock -> ``ConditionExpression version = :expected`` -> VersionConflict
  (=> 412); every mutation bumps ``version``.
* multi-item atomicity (SLS-5.1) -> ``TransactWriteItems`` for complete
  (task+commit+event) and supersedes (relation + dst flip).

Settings (table/region/endpoint/credentials) are read from ``os.environ`` here in
the storage layer — ``app/config.py`` is intentionally left untouched.

No user input is ever formatted into a DynamoDB *expression* string; values are
bound via ExpressionAttributeValues / the resource layer (the parameterisation
rule, applied to Dynamo).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from ..models import (
    CLAIMABLE_STATUSES,
    LeaseState,  # noqa: F401  (parity reference; leases are inline on Dynamo)
    Priority,
    RelationKind,
    TaskStatus,
)
from . import keys as K
from .changelog import CHANGELOG_NAMESPACE, epic_snapshot, task_snapshot
from .dto import (
    AgentDTO,
    ChainRunDTO,
    ChainStepDTO,
    ChangeDTO,
    CommitRefDTO,
    CounterDTO,
    DecisionDTO,
    EpicDTO,
    EventDTO,
    IdempotentOutcome,
    MemberDTO,
    NoteDTO,
    ProjectDTO,
    ProjectNoteDTO,
    ReservationDTO,
    TaskDTO,
)
from .errors import BackendUnavailable, Conflict, NotFound, VersionConflict

_DEFAULT_LEASE_TTL = int(os.environ.get("LEASE_DEFAULT_TTL", "1800"))
_CLAIMABLE = {s.value for s in CLAIMABLE_STATUSES}
_ser = TypeSerializer()


# --------------------------------------------------------------------------- #
# value coercion (float<->Decimal, Decimal->py, iso<->datetime)
# --------------------------------------------------------------------------- #
def _ddbify(v):
    """Recursively make a value DynamoDB-safe (float -> Decimal)."""
    if isinstance(v, float):
        # round-trip through str so 1000.0 stays exact.
        from decimal import Decimal
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _ddbify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_ddbify(x) for x in v]
    return v


def _pyify(v):
    """Recursively convert a read item (Decimal -> int/float)."""
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _pyify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_pyify(x) for x in v]
    return v


def _strip_none(item: dict) -> dict:
    return {k: v for k, v in item.items() if v is not None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _iso(v):
    """Normalise a timestamp to an ISO-8601 string for storage. Accepts a
    ``datetime`` (as ``ExportDocOut`` yields on load) or an already-ISO string;
    ``None`` passes through so ``_strip_none`` drops it."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# Format marker stamped on / accepted by the full-fidelity JSON document (PORT-8).
_EXPORT_FORMAT = "spec-server-full/v1"


def _uuid() -> str:
    return str(uuid.uuid4())


def _serialize_for_txn(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in _strip_none(_ddbify(item)).items()}


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class DynamoBackend:
    """``StorageBackend`` over a single DynamoDB table."""

    def __init__(self, table=None, endpoint_url=None, region=None):
        table = table or os.environ.get("DYNAMODB_TABLE", "spec-server")
        endpoint_url = endpoint_url or os.environ.get("DYNAMODB_ENDPOINT_URL")
        region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.table_name = table
        session = boto3.session.Session()
        common = {"region_name": region}
        if endpoint_url:
            common["endpoint_url"] = endpoint_url
        self._resource = session.resource("dynamodb", **common)
        self._client = session.client("dynamodb", **common)
        self.table = self._resource.Table(table)

    # ----- health ------------------------------------------------------ #
    def ping(self) -> None:
        """Cheap liveness check for /readyz: confirm the app table is reachable
        via a bounded ``DescribeTable``. Returns ``None`` on success; raises the
        neutral ``BackendUnavailable`` on any connectivity/credential error."""
        try:
            self._client.describe_table(TableName=self.table_name)
        except (ClientError, BotoCoreError) as exc:
            raise BackendUnavailable(str(exc)) from exc

    # ----- low-level helpers ------------------------------------------- #
    def _get(self, pk: str, sk: str, *, consistent: bool = False):
        # ConsistentRead makes a same-request post-write reload strong on real
        # AWS (base-table GetItem supports it; GSIs do not). DynamoDB Local is
        # always strongly consistent, so this is a no-op there.
        try:
            resp = self.table.get_item(Key={"PK": pk, "SK": sk},
                                       ConsistentRead=consistent)
        except (ClientError, BotoCoreError) as exc:  # pragma: no cover - infra
            raise BackendUnavailable(str(exc)) from exc
        return resp.get("Item")

    def _put(self, item: dict, condition=None):
        kwargs = {"Item": _strip_none(_ddbify(item))}
        if condition is not None:
            kwargs["ConditionExpression"] = condition
        self.table.put_item(**kwargs)

    def _query(self, **kwargs):
        """Query with automatic pagination -> list of items."""
        items = []
        while True:
            try:
                resp = self.table.query(**kwargs)
            except (ClientError, BotoCoreError) as exc:  # pragma: no cover
                raise BackendUnavailable(str(exc)) from exc
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return items
            kwargs["ExclusiveStartKey"] = lek

    def _query_first(self, n: int, **kwargs):
        """Query returning at most ``n`` items, pushing ``Limit`` into DynamoDB
        so a pre-sorted (e.g. newest-first) index page is not read in full.

        Only safe on an already-ordered index path with NO post-filter (Limit is
        applied by DynamoDB before any FilterExpression, so a filtered query must
        still read the whole partition — those callers use ``_query`` instead)."""
        items: list = []
        kwargs = dict(kwargs)
        if n <= 0:
            return items
        while True:
            kwargs["Limit"] = n - len(items)
            try:
                resp = self.table.query(**kwargs)
            except (ClientError, BotoCoreError) as exc:  # pragma: no cover
                raise BackendUnavailable(str(exc)) from exc
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek or len(items) >= n:
                return items[:n]
            kwargs["ExclusiveStartKey"] = lek

    def _transact(self, puts):
        """``TransactWriteItems`` of Put actions.

        ``puts`` is a list of (item, condition_expr, names, values) tuples where
        condition_expr is a plain string (already using #alias / :val), names is
        ExpressionAttributeNames, values is a plain-python dict of
        ExpressionAttributeValues (serialised here).
        """
        actions = []
        for item, cond, names, values in puts:
            put = {"TableName": self.table_name, "Item": _serialize_for_txn(item)}
            if cond:
                put["ConditionExpression"] = cond
            if names:
                put["ExpressionAttributeNames"] = names
            if values:
                put["ExpressionAttributeValues"] = {
                    k: _ser.serialize(_ddbify(v)) for k, v in values.items()
                }
            actions.append({"Put": put})
        self._transact_raw(actions)

    def _transact_raw(self, actions):
        """``TransactWriteItems`` of pre-built action dicts (Put/Update/Delete).

        Used by the change-log write path (UI-DELTA) to bundle an entity Update or
        Delete with the change-entry Put so the two are all-or-nothing. Maps a
        cancelled transaction (a failed ConditionExpression) to the neutral
        ``Conflict`` the callers translate (VersionConflict / dedupe / claim retry)."""
        try:
            self._client.transact_write_items(TransactItems=actions)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("TransactionCanceledException", "ConditionalCheckFailedException"):
                raise Conflict("transaction condition failed") from exc
            raise BackendUnavailable(str(exc)) from exc  # pragma: no cover

    # ----- projects ---------------------------------------------------- #
    def _project_item(self, slug: str):
        item = self._get(K.pk(slug), K.project_sk())
        if item is None:
            raise NotFound(f"Project '{slug}' not found.")
        return item

    def _project_dto(self, it) -> ProjectDTO:
        return ProjectDTO(
            public_id=it["public_id"], slug=it["slug"], name=it["name"],
            description=it.get("description"),
            default_branch=it.get("default_branch", "main"),
            created_at=_dt(it["created_at"]), updated_at=_dt(it["updated_at"]),
        )

    def list_projects(self) -> list[ProjectDTO]:
        rows = self._query(
            IndexName=K.GSI5,
            KeyConditionExpression=Key("GSI5PK").eq(K.gsi5_pk()),
        )
        rows.sort(key=lambda r: r["slug"])
        return [self._project_dto(r) for r in rows]

    def get_project(self, slug: str) -> ProjectDTO:
        return self._project_dto(self._project_item(slug))

    def create_project(self, data: dict, *, creator_sub: str | None = None,
                       creator_name: str | None = None) -> ProjectDTO:
        slug = data["slug"]
        now = _now_iso()
        item = {
            "PK": K.pk(slug), "SK": K.project_sk(), "type": "project",
            "public_id": _uuid(), "slug": slug, "name": data["name"],
            "description": data.get("description"),
            "default_branch": data.get("default_branch", "main"),
            "created_at": now, "updated_at": now,
            "GSI5PK": K.gsi5_pk(), "GSI5SK": K.gsi5_sk(slug),
        }
        if not creator_sub:
            # No authenticated identity (local/auth-off): unchanged single-put
            # path — byte-for-byte identical to pre-ISO-4 behaviour.
            try:
                self._put(item, condition=Attr("PK").not_exists())
            except ClientError as exc:
                if _is_conditional(exc):
                    raise Conflict(f"Project '{slug}' already exists.") from exc
                raise BackendUnavailable(str(exc)) from exc  # pragma: no cover
            return self._project_dto(item)

        # Creator-auto-admin (ISO-4): write the project row AND the creator's
        # ``admin`` membership atomically (TransactWriteItems) — a project must
        # never exist without an admin member (that is a lockout). The project
        # put keeps the attribute_not_exists(PK) guard, so a duplicate slug fails
        # the whole transaction -> Conflict, exactly like the single-put path.
        member = {
            "PK": K.pk(slug), "SK": K.member_sk(creator_sub),
            "type": "member", "project_slug": slug,
            "principal_sub": creator_sub, "principal_name": creator_name,
            "role": "admin", "created_at": now,
            "GSI6PK": K.gsi6_member_pk(creator_sub), "GSI6SK": K.gsi6_sk(slug),
        }
        try:
            self._transact([
                (item, "attribute_not_exists(PK)", None, None),
                (member, None, None, None),
            ])
        except Conflict as exc:
            raise Conflict(f"Project '{slug}' already exists.") from exc
        return self._project_dto(item)

    def update_project(self, slug: str, patch: dict) -> ProjectDTO:
        item = self._project_item(slug)
        for k, v in patch.items():
            item[k] = v
        item["updated_at"] = _now_iso()
        self._put(item)
        return self._project_dto(item)

    def delete_project(self, slug: str) -> None:
        self._project_item(slug)  # 404 if absent
        rows = self._query(KeyConditionExpression=Key("PK").eq(K.pk(slug)))
        with self.table.batch_writer() as bw:
            for r in rows:
                bw.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})

    # ----- agents ------------------------------------------------------ #
    def _agent_dto(self, slug, it) -> AgentDTO:
        return AgentDTO(
            public_id=it["public_id"], project=slug, slug=it["slug"],
            display_name=it.get("display_name"), kind=it.get("kind", "agent"),
            created_at=_dt(it["created_at"]),
        )

    def list_agents(self, slug: str) -> list[AgentDTO]:
        self._project_item(slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("AGENT#")
        )
        rows.sort(key=lambda r: r["slug"])
        return [self._agent_dto(slug, r) for r in rows]

    def upsert_agent(self, slug: str, data: dict) -> AgentDTO:
        self._project_item(slug)
        aslug = data["slug"]
        existing = self._get(K.pk(slug), K.agent_sk(aslug))
        if existing is None:
            item = {
                "PK": K.pk(slug), "SK": K.agent_sk(aslug), "type": "agent",
                "public_id": _uuid(), "slug": aslug,
                "display_name": data.get("display_name"),
                "kind": data.get("kind", "agent"), "created_at": _now_iso(),
            }
        else:
            item = existing
            for k, v in data.items():
                item[k] = v
        self._put(item)
        return self._agent_dto(slug, item)

    # ----- project membership (ISO-1; dormant) ------------------------- #
    def _member_dto(self, it) -> MemberDTO:
        return MemberDTO(
            project_slug=it["project_slug"], principal_sub=it["principal_sub"],
            principal_name=it.get("principal_name"), role=it["role"],
            created_at=_dt(it["created_at"]),
        )

    def get_membership(self, project_slug: str, principal_sub: str) -> MemberDTO | None:
        self._project_item(project_slug)
        it = self._get(K.pk(project_slug), K.member_sk(principal_sub))
        return self._member_dto(it) if it is not None else None

    def list_members(self, project_slug: str) -> list[MemberDTO]:
        self._project_item(project_slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(project_slug))
            & Key("SK").begins_with(K.member_prefix())
        )
        rows.sort(key=lambda r: r["principal_sub"])
        return [self._member_dto(r) for r in rows]

    def add_member(self, project_slug: str, principal_sub: str,
                   principal_name: str | None, role: str) -> MemberDTO:
        self._project_item(project_slug)
        existing = self._get(K.pk(project_slug), K.member_sk(principal_sub))
        # idempotent upsert: keep the original created_at, update role/name.
        created_at = existing["created_at"] if existing else _now_iso()
        item = {
            "PK": K.pk(project_slug), "SK": K.member_sk(principal_sub),
            "type": "member", "project_slug": project_slug,
            "principal_sub": principal_sub, "principal_name": principal_name,
            "role": role, "created_at": created_at,
            "GSI6PK": K.gsi6_member_pk(principal_sub),
            "GSI6SK": K.gsi6_sk(project_slug),
        }
        self._put(item)
        return self._member_dto(item)

    def remove_member(self, project_slug: str, principal_sub: str) -> None:
        self._project_item(project_slug)
        # idempotent: deleting an absent item is a no-op on DynamoDB.
        self.table.delete_item(
            Key={"PK": K.pk(project_slug), "SK": K.member_sk(principal_sub)}
        )

    def list_projects_for_principal(self, principal_sub: str) -> list[MemberDTO]:
        rows = self._query(
            IndexName=K.GSI6,
            KeyConditionExpression=Key("GSI6PK").eq(
                K.gsi6_member_pk(principal_sub)),
        )
        rows.sort(key=lambda r: r["project_slug"])
        return [self._member_dto(r) for r in rows]

    # ----- epics ------------------------------------------------------- #
    def _epic_item(self, slug: str, key: str):
        item = self._get(K.pk(slug), K.epic_sk(key))
        if item is None:
            raise NotFound(f"Epic '{key}' not found.")
        return item

    def _epic_dto(self, it) -> EpicDTO:
        return EpicDTO(
            public_id=it["public_id"], key=it["key"], title=it["title"],
            description=it.get("description"), section=it.get("section", "backlog"),
            position=_pyify(it.get("position", 1000.0)),
        )

    def list_epics(self, slug: str) -> list[EpicDTO]:
        self._project_item(slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("EPIC#"),
            FilterExpression=Attr("type").eq("epic"),
        )
        rows.sort(key=lambda r: (_pyify(r.get("position", 1000.0)), r["key"]))
        return [self._epic_dto(r) for r in rows]

    def create_epic(self, slug: str, data: dict) -> EpicDTO:
        self._project_item(slug)
        now = _now_iso()
        item = {
            "PK": K.pk(slug), "SK": K.epic_sk(data["key"]), "type": "epic",
            "public_id": _uuid(), "key": data["key"], "title": data["title"],
            "description": data.get("description"),
            "section": data.get("section", "backlog"),
            "position": data.get("position", 1000.0),
            "created_at": now, "updated_at": now,
        }
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "epic", item["public_id"], "upsert",
                                   None, epic_snapshot(self._epic_dto(item)))
        try:
            self._transact([
                (item, "attribute_not_exists(PK)", None, None),
                (change, None, None, None),
            ])
        except Conflict as exc:
            raise Conflict(f"Epic '{data['key']}' already exists.") from exc
        return self._epic_dto(item)

    def get_epic(self, slug: str, key: str) -> EpicDTO:
        self._project_item(slug)
        return self._epic_dto(self._epic_item(slug, key))

    def update_epic(self, slug: str, key: str, patch: dict) -> EpicDTO:
        self._project_item(slug)
        item = self._epic_item(slug, key)
        for k, v in patch.items():
            item[k] = v
        item["updated_at"] = _now_iso()
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "epic", item["public_id"], "upsert",
                                   None, epic_snapshot(self._epic_dto(item)))
        self._transact([(item, None, None, None), (change, None, None, None)])
        return self._epic_dto(item)

    def list_epic_notes(self, slug: str, key: str) -> list[NoteDTO]:
        self._project_item(slug)
        self._epic_item(slug, key)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(f"EPIC#{key}#NOTE#")
        )
        return [self._note_dto(r) for r in rows]

    def append_epic_note(self, slug: str, key: str, data: dict) -> NoteDTO:
        self._project_item(slug)
        epic = self._epic_item(slug, key)
        note_item = self._build_note_item(slug, kind="epic", ref_key=key,
                                          epic_key=key, data=data)
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "epic", epic["public_id"], "upsert",
                                   None, epic_snapshot(self._epic_dto(epic)))
        self._transact([(note_item, None, None, None), (change, None, None, None)])
        self._emit_event(slug, "note", agent=data.get("author"),
                         message=f"note on epic {key}: {data['body'][:120]}")
        return self._note_dto(note_item)

    # ----- notes (shared) ---------------------------------------------- #
    def _note_dto(self, it) -> NoteDTO:
        return NoteDTO(author=it.get("author"), body=it["body"],
                       created_at=_dt(it["created_at"]))

    def _build_note_item(self, slug, *, kind, ref_key, data,
                         task_display=None, epic_key=None):
        """Build (do not write) a note item; the caller writes it together with its
        parent-entity change entry in one TransactWriteItems (UI-DELTA atomicity)."""
        ts = _now().isoformat()
        uid = _uuid()
        if kind == "task":
            sk = K.task_note_sk(ref_key, ts, uid)
            feed = K.FEED_TASK_NOTE
            ntype = "task_note"
        else:
            sk = K.epic_note_sk(ref_key, ts, uid)
            feed = K.FEED_EPIC_NOTE
            ntype = "epic_note"
        return {
            "PK": K.pk(slug), "SK": sk, "type": ntype,
            "author": data.get("author"), "body": data["body"],
            "created_at": ts,
            "scope": "task" if kind == "task" else "epic",
            "task": task_display, "epic": epic_key,
            "GSI4PK": K.gsi4_feed_pk(slug, feed), "GSI4SK": K.gsi4_sk(ts, uid),
        }

    # ----- tasks: item build / dto ------------------------------------- #
    def _priority_rank(self, priority):
        if not priority:
            return K.NO_PRIORITY_RANK
        return K.PRIORITY_RANK.get(priority, K.NO_PRIORITY_RANK)

    def _apply_task_gsi(self, item: dict) -> dict:
        """(Re)compute GSI attributes from the task's current fields."""
        slug = item["project_slug"]
        pubid = item["public_id"]
        status = item["status"]
        rank = self._priority_rank(item.get("priority"))
        pos = _pyify(item.get("position", 1000.0))
        item["priority_rank"] = rank
        item["GSI1PK"] = K.gsi1_status_pk(slug, status)
        item["GSI1SK"] = K.gsi1_sk(rank, pos, pubid)
        # sparse key index
        if item.get("key"):
            item["GSI3PK"] = K.gsi3_key_pk(slug, item["key"])
            item["GSI3SK"] = K.gsi3_sk(pubid)
        else:
            item.pop("GSI3PK", None)
            item.pop("GSI3SK", None)
        # sparse owner index
        if item.get("owner"):
            item["GSI2PK"] = K.gsi2_owner_pk(slug, item["owner"])
            item["GSI2SK"] = K.gsi2_sk(pubid)
        else:
            item.pop("GSI2PK", None)
            item.pop("GSI2SK", None)
            item.pop("owner", None)
        return item

    def _load_task_full(self, slug: str, pubid: str, *, consistent: bool = False):
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.task_prefix(pubid)),
            ConsistentRead=consistent,
        )
        base = None
        commits, notes = [], []
        for r in rows:
            sk = r["SK"]
            if sk == K.task_sk(pubid):
                base = r
            elif "#COMMIT#" in sk:
                commits.append(r)
            elif "#NOTE#" in sk:
                notes.append(r)
        if base is None:
            raise NotFound(f"Task '{pubid}' not found.")
        return base, commits, notes

    def _get_task_base(self, slug: str, ident: str, *, consistent: bool = False):
        """Resolve a task by human key (GSI3) then public_id (GetItem).

        The GSI3 key lookup is always eventually consistent (GSIs cannot be read
        strongly), but the authoritative item is then fetched from the base table
        where ``consistent`` yields a strong read-after-write for mutators."""
        rows = self._query(
            IndexName=K.GSI3,
            KeyConditionExpression=Key("GSI3PK").eq(K.gsi3_key_pk(slug, ident)),
        )
        if rows:
            pubid = rows[0]["SK"].split("#", 1)[1]
            base = self._get(K.pk(slug), K.task_sk(pubid), consistent=consistent)
            if base is not None:
                return base
        base = self._get(K.pk(slug), K.task_sk(ident), consistent=consistent)
        if base is None:
            raise NotFound(f"Task '{ident}' not found.")
        return base

    def _task_dto(self, base, commits=None, notes=None) -> TaskDTO:
        commits = commits or []
        notes = notes or []
        commits = sorted(commits, key=lambda c: c["created_at"])
        notes = sorted(notes, key=lambda n: n["created_at"])
        priority = base.get("priority")
        return TaskDTO(
            public_id=base["public_id"],
            display_id=base.get("key") or base["public_id"],
            key=base.get("key"), epic_key=base.get("epic_key"),
            title=base["title"], description=base.get("description"),
            status=TaskStatus(base["status"]),
            priority=Priority(priority) if priority else None,
            component=base.get("component"), proof_cmd=base.get("proof_cmd"),
            status_note=base.get("status_note"),
            section=base.get("section", "backlog"),
            owner=base.get("owner"),
            lease_expires_at=_dt(base.get("lease_expires_at")),
            position=_pyify(base.get("position", 1000.0)),
            version=int(_pyify(base.get("version", 1))),
            tags=list(base.get("tags", [])),
            commits=[CommitRefDTO(sha=c["sha"], repo=c.get("repo"),
                                  test_summary=c.get("test_summary"),
                                  created_at=_dt(c["created_at"])) for c in commits],
            notes=[self._note_dto(n) for n in notes],
            created_at=_dt(base.get("created_at")),
            updated_at=_dt(base.get("updated_at")),
            completed_at=_dt(base.get("completed_at")),
        )

    def _dto_for(self, slug, base, *, consistent: bool = False) -> TaskDTO:
        base, commits, notes = self._load_task_full(
            slug, base["public_id"], consistent=consistent)
        return self._task_dto(base, commits, notes)

    # ----- tasks: CRUD ------------------------------------------------- #
    def list_tasks(self, slug: str, flt: dict) -> list[TaskDTO]:
        self._project_item(slug)
        epic_key = None
        if "epic" in flt:
            self._epic_item(slug, flt["epic"])  # 404 if absent
            epic_key = flt["epic"]

        if "owner" in flt:
            rows = self._query(
                IndexName=K.GSI2,
                KeyConditionExpression=Key("GSI2PK").eq(
                    K.gsi2_owner_pk(slug, flt["owner"])),
            )
        elif "status" in flt:
            rows = self._query(
                IndexName=K.GSI1,
                KeyConditionExpression=Key("GSI1PK").eq(
                    K.gsi1_status_pk(slug, flt["status"])),
            )
        else:
            rows = self._query(
                KeyConditionExpression=Key("PK").eq(K.pk(slug))
                & Key("SK").begins_with("TASK#"),
                FilterExpression=Attr("type").eq("task"),
            )

        def keep(r):
            if r.get("type") != "task" and "TASK#" in r["SK"] and "#" in r["SK"][5:]:
                return False
            if "status" in flt and r["status"] != flt["status"]:
                return False
            if "owner" in flt and r.get("owner") != flt["owner"]:
                return False
            if "priority" in flt and r.get("priority") != flt["priority"]:
                return False
            if epic_key is not None and r.get("epic_key") != epic_key:
                return False
            if "tag" in flt and flt["tag"] not in (r.get("tags") or []):
                return False
            if "q" in flt:
                needle = flt["q"].lower()
                hay = f"{r.get('title', '')} {r.get('description') or ''}".lower()
                if needle not in hay:
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        rows.sort(key=lambda r: (_pyify(r.get("position", 1000.0)),
                                 r.get("created_at", ""), r["public_id"]))
        offset, limit = flt["offset"], flt["limit"]
        rows = rows[offset:offset + limit]
        return [self._dto_for(slug, r) for r in rows]

    def create_task(self, slug: str, data: dict) -> TaskDTO:
        self._project_item(slug)
        data = dict(data)
        tags = data.pop("tags", []) or []
        epic_key = data.pop("epic_key", None)
        if epic_key:
            self._epic_item(slug, epic_key)  # 404 if absent
        key = data.get("key")
        if key:
            existing = self._query(
                IndexName=K.GSI3,
                KeyConditionExpression=Key("GSI3PK").eq(K.gsi3_key_pk(slug, key)),
            )
            if existing:
                raise Conflict(f"Task key '{key}' already exists.")
        pubid = _uuid()
        now = _now_iso()
        item = {
            "PK": K.pk(slug), "SK": K.task_sk(pubid), "type": "task",
            "project_slug": slug, "public_id": pubid, "key": key,
            "epic_key": epic_key, "title": data["title"],
            "description": data.get("description"),
            "status": data.get("status", "todo"),
            "priority": data.get("priority"),
            "component": data.get("component"), "proof_cmd": data.get("proof_cmd"),
            "status_note": None, "section": data.get("section", "backlog"),
            "owner": None, "lease_expires_at": None,
            "position": data.get("position", 1000.0), "version": 1,
            "tags": tags, "created_by": data.get("created_by"),
            "created_at": now, "updated_at": now, "completed_at": None,
        }
        self._apply_task_gsi(item)
        # Write the task and its change entry all-or-nothing (UI-DELTA). The
        # attribute_not_exists(PK) guard still trips on a (near-impossible) pubid
        # collision; duplicate human keys are already rejected above before any
        # seq is allocated, so the seq stays gap-free.
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "task", pubid, "upsert", 1,
                                   task_snapshot(self._task_dto(item)))
        self._transact([
            (item, "attribute_not_exists(PK)", None, None),
            (change, None, None, None),
        ])
        return self._task_dto(item)

    def get_task(self, slug: str, ident: str) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident)
        return self._dto_for(slug, base)

    def update_task(self, slug: str, ident: str, patch: dict,
                    expected_version: str | None) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        cur_v = int(_pyify(base["version"]))
        _check_version(cur_v, expected_version)
        data = dict(patch)
        if "epic_key" in data:
            ek = data.pop("epic_key")
            if ek:
                self._epic_item(slug, ek)
            base["epic_key"] = ek
        for k, v in data.items():
            base[k] = v
        base["version"] = cur_v + 1
        base["updated_at"] = _now_iso()
        self._apply_task_gsi(base)
        self._put_task_versioned_with_change(slug, base, cur_v)
        return self._dto_for(slug, base, consistent=True)

    def delete_task(self, slug: str, ident: str) -> None:
        self._project_item(slug)
        base = self._get_task_base(slug, ident)
        pubid = base["public_id"]
        # Atomically remove the base task item AND write the delete tombstone
        # (UI-DELTA) so a delta client can never miss the eviction. The task's
        # child items (commits/notes/relations) are cleaned up best-effort after —
        # once the base row is gone they never surface as a task.
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "task", pubid, "delete")
        self._transact_raw([
            {"Delete": {"TableName": self.table_name,
                        "Key": {"PK": _ser.serialize(K.pk(slug)),
                                "SK": _ser.serialize(K.task_sk(pubid))}}},
            {"Put": {"TableName": self.table_name,
                     "Item": _serialize_for_txn(change)}},
        ])
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.task_prefix(pubid))
        )
        with self.table.batch_writer() as bw:
            for r in rows:
                bw.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})

    def _put_task_versioned_with_change(self, slug: str, item: dict, expected: int):
        """Version-guarded task Put + its change entry in ONE TransactWriteItems
        (UI-DELTA), so the mutation and the change commit all-or-nothing. ``item``
        already carries the bumped version; a lost update (someone else bumped
        first) cancels the whole transaction -> VersionConflict, and NO change is
        written."""
        seq = self._next_change_seq(slug)
        change = self._change_item(
            slug, seq, "task", item["public_id"], "upsert",
            int(_pyify(item["version"])), task_snapshot(self._task_dto(item)))
        try:
            self._transact([
                (item, "#v = :expected", {"#v": "version"}, {":expected": expected}),
                (change, None, None, None),
            ])
        except Conflict as exc:
            raise VersionConflict("Version conflict: re-read and retry.") from exc

    # ----- tasks: atomic guarantees + lifecycle ------------------------ #
    def claim_next(self, slug: str, agent: str, *, epic=None, priority_max=None,
                   component=None, lease_ttl=None, idempotency_key=None,
                   serialize=None) -> IdempotentOutcome:
        self._project_item(slug)
        if idempotency_key:
            stored = self._lookup_idem(slug, "claim-next", idempotency_key)
            if stored is not None:
                return IdempotentOutcome(replay_body=stored["response_json"],
                                         replay_status=int(stored["status_code"]))
        if epic:
            self._epic_item(slug, epic)
        ttl = _DEFAULT_LEASE_TTL if lease_ttl is None else lease_ttl
        expires = _now() + timedelta(seconds=ttl)

        base = self._claim_candidate(slug, agent, expires, epic=epic,
                                     component=component, priority_max=priority_max)
        if base is None:
            return IdempotentOutcome()
        self._emit_event(slug, "claimed", agent=agent,
                         message=f"{agent} claimed {base.get('key') or base['public_id']}")
        dto = self._dto_for(slug, base, consistent=True)
        if idempotency_key and serialize is not None:
            self._store_idem(slug, "claim-next", idempotency_key,
                             serialize(dto), 200)
        return IdempotentOutcome(result=dto)

    def _claim_candidate(self, slug, agent, expires, *, epic, component,
                         priority_max):
        exp_iso = expires.isoformat()
        now_iso = _now_iso()

        # --- 1) fresh todo tasks (priority/position order via GSI1) ---
        todo_filter = None
        if epic:
            todo_filter = Attr("epic_key").eq(epic)
        if component:
            f = Attr("component").eq(component)
            todo_filter = f if todo_filter is None else todo_filter & f
        if priority_max:
            cutoff = K.PRIORITY_RANK[priority_max]
            f = Attr("priority_rank").lte(cutoff)
            todo_filter = f if todo_filter is None else todo_filter & f

        won = self._try_claim_partition(
            slug, status="todo", agent=agent, exp_iso=exp_iso,
            extra_filter=todo_filter,
            condition="(attribute_not_exists(#owner) OR #owner = :agent) "
                      "AND #status = :cur",
            values={":cur": "todo"},
            forward=True,
        )
        if won is not None:
            return won

        # --- 2) reclaim expired in_progress leases ---
        reclaim_filter = Attr("lease_expires_at").lt(now_iso)
        if epic:
            reclaim_filter = reclaim_filter & Attr("epic_key").eq(epic)
        if component:
            reclaim_filter = reclaim_filter & Attr("component").eq(component)
        if priority_max:
            reclaim_filter = reclaim_filter & Attr("priority_rank").lte(
                K.PRIORITY_RANK[priority_max])
        won = self._try_claim_partition(
            slug, status="in_progress", agent=agent, exp_iso=exp_iso,
            extra_filter=reclaim_filter,
            condition="#status = :cur AND lease_expires_at < :now",
            values={":cur": "in_progress", ":now": now_iso},
            forward=True,
        )
        return won

    def _try_claim_partition(self, slug, *, status, agent, exp_iso, extra_filter,
                             condition, values, forward):
        kwargs = {
            "IndexName": K.GSI1,
            "KeyConditionExpression": Key("GSI1PK").eq(
                K.gsi1_status_pk(slug, status)),
            "ScanIndexForward": forward,
        }
        if extra_filter is not None:
            kwargs["FilterExpression"] = extra_filter
        candidates = self._query(**kwargs)
        names = {"#owner": "owner", "#status": "status", "#v": "version"}
        base_values = {":agent": agent, ":inprog": "in_progress",
                       ":exp": exp_iso, ":now2": _now_iso(), ":one": 1}
        base_values.update(values)
        for cand in candidates:
            pubid = cand["public_id"]
            upd = ("SET #status = :inprog, #owner = :agent, "
                   "lease_expires_at = :exp, updated_at = :now2, "
                   "GSI1PK = :g1pk, GSI2PK = :g2pk, GSI2SK = :g2sk ADD #v :one")
            vals = dict(base_values)
            vals[":g1pk"] = K.gsi1_status_pk(slug, "in_progress")
            vals[":g2pk"] = K.gsi2_owner_pk(slug, agent)
            vals[":g2sk"] = K.gsi2_sk(pubid)
            # Claim the candidate AND write its change entry all-or-nothing
            # (UI-DELTA): the conditional Update (exactly-one-winner) rides a
            # TransactWriteItems with the change Put, so a claim that loses the
            # race cancels the transaction and writes NO change (the just-allocated
            # seq is skipped — a rare, harmless gap, never a duplicate). The
            # winner's change carries the post-claim state (in_progress, owner,
            # bumped version) reconstructed from the candidate + the SET values.
            seq = self._next_change_seq(slug)
            won_base = dict(cand)
            won_base["status"] = "in_progress"
            won_base["owner"] = agent
            won_base["lease_expires_at"] = exp_iso
            won_base["updated_at"] = vals[":now2"]
            won_base["version"] = int(_pyify(cand["version"])) + 1
            change = self._change_item(
                slug, seq, "task", pubid, "upsert", won_base["version"],
                task_snapshot(self._task_dto(won_base)))
            update_action = {"Update": {
                "TableName": self.table_name,
                "Key": {"PK": _ser.serialize(K.pk(slug)),
                        "SK": _ser.serialize(K.task_sk(pubid))},
                "UpdateExpression": upd,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {
                    k: _ser.serialize(_ddbify(v)) for k, v in vals.items()},
            }}
            put_action = {"Put": {"TableName": self.table_name,
                                  "Item": _serialize_for_txn(change)}}
            try:
                self._transact_raw([update_action, put_action])
                return won_base
            except Conflict:
                continue  # someone else won this one -> try next candidate
        return None

    def complete_task(self, slug: str, ident: str, data: dict,
                      expected_version: str | None) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        cur_v = int(_pyify(base["version"]))
        _check_version(cur_v, expected_version)
        pubid = base["public_id"]
        base["status"] = TaskStatus.done.value
        base["completed_at"] = _now_iso()
        base["updated_at"] = _now_iso()
        base["lease_expires_at"] = None
        base["owner"] = None
        if data.get("proof_cmd"):
            base["proof_cmd"] = data["proof_cmd"]
        base["version"] = cur_v + 1
        self._apply_task_gsi(base)

        puts = [(base, "#v = :expected", {"#v": "version"}, {":expected": cur_v})]
        if data.get("commit_sha"):
            puts.append((
                {"PK": K.pk(slug), "SK": K.commit_sk(pubid, data["commit_sha"]),
                 "type": "commit", "sha": data["commit_sha"],
                 "repo": data.get("repo"), "test_summary": data.get("test_summary"),
                 "created_at": _now_iso()},
                None, None, None,
            ))
        ts = _now().isoformat()
        uid = _uuid()
        payload = {k: v for k, v in data.items() if v}
        puts.append((
            self._event_item(slug, "completed", ts, uid,
                             message=f"completed {base.get('key') or pubid}",
                             payload=payload, task_pubid=pubid,
                             task_key=base.get("key")),
            None, None, None,
        ))
        seq = self._next_change_seq(slug)
        puts.append((
            self._change_item(slug, seq, "task", pubid, "upsert",
                              int(_pyify(base["version"])),
                              task_snapshot(self._task_dto(base))),
            None, None, None,
        ))
        try:
            self._transact(puts)
        except Conflict as exc:
            raise VersionConflict("Version conflict: re-read and retry.") from exc
        return self._dto_for(slug, base, consistent=True)

    def release_task(self, slug: str, ident: str, reset_to: str) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        cur_v = int(_pyify(base["version"]))
        base["status"] = TaskStatus(reset_to).value
        base["owner"] = None
        base["lease_expires_at"] = None
        base["version"] = cur_v + 1
        base["updated_at"] = _now_iso()
        self._apply_task_gsi(base)
        self._put_task_versioned_with_change(slug, base, cur_v)
        return self._dto_for(slug, base, consistent=True)

    def set_status(self, slug: str, ident: str, status: str, note, has_note: bool,
                   expected_version: str | None) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        cur_v = int(_pyify(base["version"]))
        _check_version(cur_v, expected_version)
        base["status"] = TaskStatus(status).value
        if has_note:
            base["status_note"] = note
        if base["status"] == TaskStatus.done.value:
            base["completed_at"] = _now_iso()
        base["version"] = cur_v + 1
        base["updated_at"] = _now_iso()
        self._apply_task_gsi(base)
        self._put_task_versioned_with_change(slug, base, cur_v)
        return self._dto_for(slug, base, consistent=True)

    def add_commit(self, slug: str, ident: str, data: dict) -> TaskDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        pubid = base["public_id"]
        item = {
            "PK": K.pk(slug), "SK": K.commit_sk(pubid, data["sha"]),
            "type": "commit", "sha": data["sha"], "repo": data.get("repo"),
            "test_summary": data.get("test_summary"), "created_at": _now_iso(),
        }
        # A duplicate (task, sha) is a genuine no-op (dedupe) — pre-check so a
        # duplicate allocates no seq (gap-free, parity with Postgres). A new commit
        # writes the commit row + a task upsert change all-or-nothing.
        if self._get(K.pk(slug), K.commit_sk(pubid, data["sha"])) is None:
            seq = self._next_change_seq(slug)
            change = self._change_item(
                slug, seq, "task", pubid, "upsert",
                int(_pyify(base["version"])), task_snapshot(self._task_dto(base)))
            try:
                self._transact([
                    (item, "attribute_not_exists(SK)", None, None),
                    (change, None, None, None),
                ])
            except Conflict:
                # A concurrent identical commit won the SK race -> dedupe silently
                # (a rare, harmless seq gap — never a duplicate value).
                pass
        return self._dto_for(slug, base, consistent=True)

    def list_task_notes(self, slug: str, ident: str) -> list[NoteDTO]:
        self._project_item(slug)
        base = self._get_task_base(slug, ident)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(f"TASK#{base['public_id']}#NOTE#")
        )
        return [self._note_dto(r) for r in rows]

    def append_task_note(self, slug: str, ident: str, data: dict) -> NoteDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident)
        display = base.get("key") or base["public_id"]
        note_item = self._build_note_item(slug, kind="task",
                                          ref_key=base["public_id"], data=data,
                                          task_display=display)
        seq = self._next_change_seq(slug)
        change = self._change_item(slug, seq, "task", base["public_id"], "upsert",
                                   int(_pyify(base["version"])),
                                   task_snapshot(self._task_dto(base)))
        self._transact([(note_item, None, None, None), (change, None, None, None)])
        self._emit_event(slug, "note", agent=data.get("author"),
                         task_pubid=base["public_id"], task_key=base.get("key"),
                         message=f"note on {display}: {data['body'][:120]}")
        return self._note_dto(note_item)

    def add_relation(self, slug: str, ident: str, target: str, kind: str) -> str:
        self._project_item(slug)
        src = self._get_task_base(slug, ident, consistent=True)
        dst = self._get_task_base(slug, target, consistent=True)
        kind_enum = RelationKind(kind)
        rel = {
            "PK": K.pk(slug),
            "SK": K.relation_sk(src["public_id"], kind_enum.value, dst["public_id"]),
            "type": "relation", "kind": kind_enum.value,
            "src": src["public_id"], "dst": dst["public_id"],
            "created_at": _now_iso(),
        }
        src_disp = src.get("key") or src["public_id"]
        dst_disp = dst.get("key") or dst["public_id"]
        # One change for the relation's subject (source) task, written in the same
        # TransactWriteItems as the relation (and the dst flip, for supersedes).
        seq = self._next_change_seq(slug)
        change = self._change_item(
            slug, seq, "task", src["public_id"], "upsert",
            int(_pyify(src["version"])), task_snapshot(self._task_dto(src)))
        if kind_enum == RelationKind.supersedes:
            cur_v = int(_pyify(dst["version"]))
            dst["status"] = TaskStatus.superseded.value
            dst["superseded_by"] = src["public_id"]
            dst["version"] = cur_v + 1
            dst["updated_at"] = _now_iso()
            dst["owner"] = None
            dst["lease_expires_at"] = None
            self._apply_task_gsi(dst)
            self._transact([
                (rel, None, None, None),
                (dst, "#v = :expected", {"#v": "version"}, {":expected": cur_v}),
                (change, None, None, None),
            ])
        else:
            self._transact([
                (rel, None, None, None),
                (change, None, None, None),
            ])
        return f"{src_disp} {kind_enum.value} {dst_disp}"

    # ----- reservations / counters ------------------------------------- #
    def reserve_number(self, slug: str, namespace: str, *, reserved_by=None,
                       task_key=None, note=None, idempotency_key=None,
                       serialize=None) -> IdempotentOutcome:
        self._project_item(slug)
        if idempotency_key:
            stored = self._lookup_idem(slug, "reserve", idempotency_key)
            if stored is not None:
                return IdempotentOutcome(replay_body=stored["response_json"],
                                         replay_status=int(stored["status_code"]))
        task_pubid = None
        if task_key:
            task_pubid = self._get_task_base(slug, task_key)["public_id"]

        # Atomic, per-item serialised increment -> distinct increasing value.
        #
        # Safe-window note (SLS-13): the split "ADD counter" then
        # "TransactWriteItems(reservation + event)" cannot lose the UNIQUENESS or
        # MONOTONICITY guarantee (invariant #2): the counter ADD is serialised by
        # DynamoDB per item, so every caller reads back a distinct, strictly
        # increasing value, and the audit row is written under
        # attribute_not_exists(SK) (the UNIQUE(namespace,value) backstop). The
        # ADD cannot be folded into the TransactWriteItems because a transaction
        # returns no attribute values, so the just-allocated value could not be
        # read back to build the RES#<value> SK. The ONLY failure mode of the
        # split is a CONTIGUITY gap: if the process dies between the ADD and the
        # transact, value N is consumed without an audit row (a skipped number,
        # never a duplicate). Reservations are an append-only allocator where a
        # gap is harmless, so this window is accepted rather than closed.
        try:
            resp = self.table.update_item(
                Key={"PK": K.pk(slug), "SK": K.counter_sk(namespace)},
                UpdateExpression="ADD current_value :one",
                ExpressionAttributeValues=_ddbify({":one": 1}),
                ReturnValues="UPDATED_NEW",
            )
        except (ClientError, BotoCoreError) as exc:  # pragma: no cover
            raise BackendUnavailable(str(exc)) from exc
        value = int(_pyify(resp["Attributes"]["current_value"]))
        now = _now_iso()

        reservation = {
            "PK": K.pk(slug), "SK": K.reservation_sk(namespace, value),
            "type": "reservation", "namespace": namespace, "value": value,
            "reserved_by": reserved_by, "task": task_pubid, "note": note,
            "created_at": now,
        }
        ts = _now().isoformat()
        uid = _uuid()
        event = self._event_item(
            slug, "reserved", ts, uid, agent=reserved_by,
            message=f"reserved {namespace} #{value}",
            payload={"namespace": namespace, "value": value},
            task_pubid=task_pubid,
        )
        # audit row + event atomically; UNIQUE(namespace,value) backstop.
        self._transact([
            (reservation, "attribute_not_exists(SK)", None, None),
            (event, None, None, None),
        ])
        dto = ReservationDTO(namespace=namespace, value=value,
                             reserved_by=reserved_by, note=note,
                             created_at=_dt(now))
        if idempotency_key and serialize is not None:
            self._store_idem(slug, "reserve", idempotency_key, serialize(dto), 201)
        return IdempotentOutcome(result=dto)

    def list_reservations(self, slug: str, namespace) -> list[ReservationDTO]:
        self._project_item(slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.reservation_prefix(namespace))
        )
        rows.sort(key=lambda r: (r["namespace"], int(_pyify(r["value"]))))
        return [ReservationDTO(namespace=r["namespace"],
                               value=int(_pyify(r["value"])),
                               reserved_by=r.get("reserved_by"), note=r.get("note"),
                               created_at=_dt(r["created_at"])) for r in rows]

    def list_counters(self, slug: str) -> list[CounterDTO]:
        self._project_item(slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("COUNTER#")
        )
        # The counter item is created by the atomic ADD upsert, which sets only
        # current_value; its namespace is carried in the SK (COUNTER#<ns>).
        out = [CounterDTO(namespace=r["SK"].split("#", 1)[1],
                          current_value=int(_pyify(r["current_value"])))
               for r in rows]
        out.sort(key=lambda c: c.namespace)
        return out

    # ----- change-log (UI-DELTA) --------------------------------------- #
    def _next_change_seq(self, slug: str) -> int:
        """Allocate the next per-project change ``seq`` via the SAME atomic counter
        ``ADD`` that backs collision-proof reservation (namespace ``changelog``).
        The per-item ADD is serialised by DynamoDB, so every caller reads back a
        distinct, strictly increasing integer — never read-max-plus-one, and the
        same cursor semantics as the Postgres counter."""
        try:
            resp = self.table.update_item(
                Key={"PK": K.pk(slug), "SK": K.counter_sk(CHANGELOG_NAMESPACE)},
                UpdateExpression="ADD current_value :one",
                ExpressionAttributeValues=_ddbify({":one": 1}),
                ReturnValues="UPDATED_NEW",
            )
        except (ClientError, BotoCoreError) as exc:  # pragma: no cover - infra
            raise BackendUnavailable(str(exc)) from exc
        return int(_pyify(resp["Attributes"]["current_value"]))

    def _change_item(self, slug, seq, entity_type, entity_pubid, op,
                     version=None, snapshot=None):
        """Build a change item: base SK CHANGE#<padded seq> (numeric range read) +
        GSI7 keys (ascending seq feed). ``snapshot`` is the lean upsert DTO or None
        for a delete tombstone."""
        return {
            "PK": K.pk(slug), "SK": K.change_sk(seq), "type": "change",
            "seq": seq, "entity_type": entity_type, "entity_pubid": entity_pubid,
            "op": op, "version": version, "occurred_at": _now_iso(),
            "snapshot": snapshot,
            "GSI7PK": K.gsi7_changes_pk(slug), "GSI7SK": K.gsi7_sk(seq),
        }

    def _change_dto(self, it) -> ChangeDTO:
        v = it.get("version")
        snap = it.get("snapshot")
        return ChangeDTO(
            seq=int(_pyify(it["seq"])), entity_type=it["entity_type"],
            entity_pubid=it["entity_pubid"], op=it["op"],
            version=int(_pyify(v)) if v is not None else None,
            occurred_at=_dt(it["occurred_at"]),
            snapshot=_pyify(snap) if snap is not None else None,
        )

    def changes_head(self, slug: str) -> int:
        """Highest change ``seq`` for the project (0 when none). One descending
        base-table range read (CHANGE# is already in numeric seq order)."""
        self._project_item(slug)
        rows = self._query_first(
            1,
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.change_prefix()),
            ScanIndexForward=False,
        )
        return int(_pyify(rows[0]["seq"])) if rows else 0

    def list_changes(self, slug: str, since: int, limit: int) -> list[ChangeDTO]:
        """Change entries with ``seq > since`` ascending, via GSI7 (its HTTP
        endpoint lands in UI-DELTA-5). Zero-padded GSI7SK == numeric seq order."""
        self._project_item(slug)
        rows = self._query_first(
            limit,
            IndexName=K.GSI7,
            KeyConditionExpression=Key("GSI7PK").eq(K.gsi7_changes_pk(slug))
            & Key("GSI7SK").gt(K.gsi7_sk(since)),
            ScanIndexForward=True,
        )
        return [self._change_dto(r) for r in rows]

    # ----- events / notes-feed / decisions ----------------------------- #
    def _event_item(self, slug, event_type, ts, uid, *, agent=None, message=None,
                    payload=None, task_pubid=None, task_key=None):
        return {
            "PK": K.pk(slug), "SK": K.event_sk(ts, uid), "type": "event",
            "event_type": event_type, "agent": agent, "message": message,
            "payload": payload or {}, "task_pubid": task_pubid,
            "task_key": task_key, "created_at": ts,
            "GSI4PK": K.gsi4_feed_pk(slug, K.FEED_EVENT), "GSI4SK": K.gsi4_sk(ts, uid),
        }

    def _emit_event(self, slug, event_type, *, agent=None, message=None,
                    payload=None, task_pubid=None, task_key=None):
        ts = _now().isoformat()
        uid = _uuid()
        self._put(self._event_item(slug, event_type, ts, uid, agent=agent,
                                   message=message, payload=payload,
                                   task_pubid=task_pubid, task_key=task_key))

    def _event_dto(self, it) -> EventDTO:
        return EventDTO(event_type=it["event_type"], agent=it.get("agent"),
                        task_pubid=it.get("task_pubid"), message=it.get("message"),
                        payload=_pyify(it.get("payload", {})),
                        created_at=_dt(it["created_at"]))

    def create_event(self, slug: str, data: dict) -> EventDTO:
        self._project_item(slug)
        task_pubid = task_key = None
        if data.get("task_key"):
            base = self._get_task_base(slug, data["task_key"])
            task_pubid, task_key = base["public_id"], base.get("key")
        ts = _now().isoformat()
        uid = _uuid()
        item = self._event_item(slug, data["event_type"], ts, uid,
                                agent=data.get("agent"), message=data.get("message"),
                                payload=data.get("payload") or {},
                                task_pubid=task_pubid, task_key=task_key)
        self._put(item)
        return self._event_dto(item)

    def list_events(self, slug: str, flt: dict) -> list[EventDTO]:
        self._project_item(slug)
        task_pubid = None
        if "task" in flt:
            task_pubid = self._get_task_base(slug, flt["task"])["public_id"]
        offset, limit = flt["offset"], flt["limit"]
        has_filter = (
            "event_type" in flt or "agent" in flt or task_pubid is not None
        )
        query = dict(
            IndexName=K.GSI4,
            KeyConditionExpression=Key("GSI4PK").eq(
                K.gsi4_feed_pk(slug, K.FEED_EVENT)),
            ScanIndexForward=False,
        )
        # GSI4 is already newest-first; with no post-filter we only need the top
        # (offset+limit) items, so push Limit into DynamoDB instead of reading the
        # whole feed partition. A filtered query must read it all (Limit applies
        # before FilterExpression).
        rows = (self._query(**query) if has_filter
                else self._query_first(offset + limit, **query))

        def keep(r):
            if "event_type" in flt and r.get("event_type") != flt["event_type"]:
                return False
            if "agent" in flt and r.get("agent") != flt["agent"]:
                return False
            if task_pubid is not None and r.get("task_pubid") != task_pubid:
                return False
            return True

        rows = [r for r in rows if keep(r)]
        rows = rows[offset:offset + limit]
        return [self._event_dto(r) for r in rows]

    def list_project_notes(self, slug: str, flt: dict) -> list[ProjectNoteDTO]:
        self._project_item(slug)
        scope = flt["scope"]
        want_task = scope in ("task", "all") and "epic" not in flt
        want_epic = scope in ("epic", "all") and "task" not in flt
        task_display = None
        if "task" in flt:
            base = self._get_task_base(slug, flt["task"])
            task_display = base.get("key") or base["public_id"]

        offset, limit = flt["offset"], flt["limit"]
        cap = offset + limit
        has_filter = (
            "author" in flt or "epic" in flt or "since" in flt
            or task_display is not None
        )

        def _feed(kind):
            # Each GSI4 feed partition is newest-first; unfiltered, the global top
            # `cap` after merge must come from each partition's own top `cap`, so
            # push Limit and skip reading the whole partition on large projects.
            q = dict(
                IndexName=K.GSI4,
                KeyConditionExpression=Key("GSI4PK").eq(
                    K.gsi4_feed_pk(slug, kind)),
                ScanIndexForward=False,
            )
            return self._query(**q) if has_filter else self._query_first(cap, **q)

        rows = []
        if want_task:
            rows += _feed(K.FEED_TASK_NOTE)
        if want_epic:
            rows += _feed(K.FEED_EPIC_NOTE)

        def keep(r):
            if "author" in flt and r.get("author") != flt["author"]:
                return False
            if task_display is not None and r.get("task") != task_display:
                return False
            if "epic" in flt and r.get("epic") != flt["epic"]:
                return False
            if "since" in flt and _dt(r["created_at"]) < flt["since"]:
                return False
            return True

        rows = [r for r in rows if keep(r)]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        rows = rows[offset:offset + limit]
        return [ProjectNoteDTO(scope=r["scope"], task=r.get("task"),
                               epic=r.get("epic"), author=r.get("author"),
                               body=r["body"], created_at=_dt(r["created_at"]))
                for r in rows]

    def list_decisions(self, slug: str) -> list[DecisionDTO]:
        self._project_item(slug)
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("DEC#")
        )
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._decision_dto(r) for r in rows]

    def _decision_dto(self, it) -> DecisionDTO:
        return DecisionDTO(public_id=it["public_id"], key=it.get("key"),
                           title=it["title"], decision=it["decision"],
                           context=it.get("context"),
                           consequences=it.get("consequences"),
                           agent=it.get("agent"), created_at=_dt(it["created_at"]))

    def create_decision(self, slug: str, data: dict) -> DecisionDTO:
        self._project_item(slug)
        data = dict(data)
        task_key = data.pop("task_key", None)
        task_pubid = None
        if task_key:
            base = self._get_task_base(slug, task_key)
            task_pubid = base["public_id"]
        ts = _now().isoformat()
        uid = _uuid()
        item = {
            "PK": K.pk(slug), "SK": K.decision_sk(ts, uid), "type": "decision",
            "public_id": _uuid(), "key": data.get("key"), "title": data["title"],
            "decision": data["decision"], "context": data.get("context"),
            "consequences": data.get("consequences"), "agent": data.get("agent"),
            "task": task_pubid, "created_at": ts,
        }
        self._put(item)
        self._emit_event(slug, "decision", agent=data.get("agent"),
                         task_pubid=task_pubid,
                         message=f"decision: {data['title']}")
        return self._decision_dto(item)

    # ----- chains ------------------------------------------------------ #
    def _chain_run_item(self, slug, run_pubid):
        item = self._get(K.pk(slug), K.chain_run_sk(run_pubid))
        if item is None:
            raise NotFound(f"Chain run '{run_pubid}' not found.")
        return item

    def _chain_run_dto(self, slug, run) -> ChainRunDTO:
        steps = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.chain_step_prefix(run["public_id"]))
        )
        steps.sort(key=lambda s: int(_pyify(s.get("step_order", 0))))
        return ChainRunDTO(
            public_id=run["public_id"], status=run["status"],
            started_by=run.get("started_by"), started_at=_dt(run["started_at"]),
            finished_at=_dt(run.get("finished_at")),
            steps=[self._chain_step_dto(s) for s in steps],
        )

    def _chain_step_dto(self, s) -> ChainStepDTO:
        return ChainStepDTO(step_name=s["step_name"],
                            step_order=int(_pyify(s.get("step_order", 0))),
                            agent=s.get("agent"), status=s["status"],
                            skip_justification=s.get("skip_justification"),
                            output_ref=s.get("output_ref"))

    def create_chain_run(self, slug: str, ident: str, started_by) -> ChainRunDTO:
        self._project_item(slug)
        base = self._get_task_base(slug, ident, consistent=True)
        run_pubid = _uuid()
        item = {
            "PK": K.pk(slug), "SK": K.chain_run_sk(run_pubid), "type": "chain_run",
            "public_id": run_pubid, "task": base["public_id"],
            "started_by": started_by, "status": "running",
            "started_at": _now_iso(), "finished_at": None,
        }
        self._put(item)
        self._emit_event(
            slug, "chain_run", agent=started_by, task_pubid=base["public_id"],
            task_key=base.get("key"),
            message=f"chain run started for {base.get('key') or base['public_id']}",
            payload={"run": run_pubid})
        return self._chain_run_dto(slug, item)

    def list_chain_runs(self, slug: str, task_ident=None, *, limit=200,
                        offset=0) -> list[ChainRunDTO]:
        self._project_item(slug)
        task_pubid = None
        if task_ident is not None:
            task_pubid = self._get_task_base(slug, task_ident)["public_id"]
        # The CRUN# collection holds runs + their step children; the type filter
        # forces a full-partition read, so we sort/paginate client-side and only
        # materialise steps (a per-run query in _chain_run_dto) for the page.
        rows = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with(K.chain_run_prefix()),
            FilterExpression=Attr("type").eq("chain_run"),
        )
        if task_pubid is not None:
            rows = [r for r in rows if r.get("task") == task_pubid]
        # started_at desc, public_id as a stable tie-break for same-instant runs.
        rows.sort(key=lambda r: (r["started_at"], r["public_id"]), reverse=True)
        rows = rows[offset:offset + limit]
        return [self._chain_run_dto(slug, r) for r in rows]

    def get_chain_run(self, slug: str, run_pubid: str) -> ChainRunDTO:
        self._project_item(slug)
        return self._chain_run_dto(slug, self._chain_run_item(slug, run_pubid))

    def update_chain_run(self, slug: str, run_pubid: str, status) -> ChainRunDTO:
        self._project_item(slug)
        run = self._chain_run_item(slug, run_pubid)
        if status is not None:
            run["status"] = status
            if status in ("passed", "failed", "aborted"):
                run["finished_at"] = _now_iso()
        self._put(run)
        return self._chain_run_dto(slug, run)

    def upsert_chain_step(self, slug: str, run_pubid: str, step_name: str,
                          data: dict) -> ChainStepDTO:
        self._project_item(slug)
        run = self._chain_run_item(slug, run_pubid)
        item = {
            "PK": K.pk(slug), "SK": K.chain_step_sk(run_pubid, step_name),
            "type": "chain_step", "run": run_pubid, "step_name": step_name,
            "step_order": data["step_order"], "agent": data.get("agent"),
            "status": data["status"],
            "skip_justification": data.get("skip_justification"),
            "output_ref": data.get("output_ref"),
        }
        self._put(item)
        self._emit_event(
            slug, "chain_step", agent=data.get("agent"),
            task_pubid=run.get("task"),
            message=f"chain step {step_name} -> {data['status']}",
            payload={"run": run_pubid, "step": step_name, "status": data["status"]})
        return self._chain_step_dto(item)

    # ----- idempotency ------------------------------------------------- #
    def _lookup_idem(self, slug, endpoint, key):
        item = self._get(K.pk(slug), K.idempotency_sk(endpoint, key))
        if item is None:
            return None
        item["response_json"] = _pyify(item.get("response_json", {}))
        return item

    def _store_idem(self, slug, endpoint, key, response_json, status_code):
        item = {
            "PK": K.pk(slug), "SK": K.idempotency_sk(endpoint, key),
            "type": "idempotency", "endpoint": endpoint, "key": key,
            "response_json": response_json, "status_code": status_code,
            "created_at": _now_iso(),
        }
        try:
            self._put(item, condition=Attr("SK").not_exists())
        except ClientError as exc:
            if _is_conditional(exc):
                return self._lookup_idem(slug, endpoint, key)
            raise BackendUnavailable(str(exc)) from exc  # pragma: no cover
        return item

    # ----- ports ------------------------------------------------------- #
    def import_spec(self, slug: str, parsed) -> dict:
        """Idempotently upsert a parsed SPEC.md tree. Batched for full-sized
        backlogs (PORT-6): existing epics/tasks are loaded with two partition
        queries (not a per-task GSI query + GetItem), and writes go through
        ``BatchWriteItem`` (25/request, UnprocessedItems auto-retried by
        ``batch_writer``) so a ~1,500-task import is a few dozen batched requests
        instead of ~4,500 round-trips. Per-task validation errors are collected in
        ``failed`` (that row is skipped), never 500ing the whole request. Parity
        with the Postgres adapter: same counts, same idempotency, same skip-on-
        unchanged (no write, no version bump), and existing owner/lease/status/
        created_at are preserved across an update."""
        from ..specmd import validate_parsed_task

        self._project_item(slug)
        created_epics = updated_epics = 0
        created_tasks = updated_tasks = unchanged_tasks = 0
        failed: list[dict] = []
        now = _now_iso()

        # --- epics: bulk-load existing partition, upsert, batch-write ------
        existing_epics = {
            it["key"]: it for it in self._query(
                KeyConditionExpression=Key("PK").eq(K.pk(slug))
                & Key("SK").begins_with("EPIC#"),
                FilterExpression=Attr("type").eq("epic"),
            ) if it.get("key")
        }
        epic_writes = []
        for ekey, pe in parsed.epics.items():
            existing = existing_epics.get(ekey)
            item = existing or {
                "PK": K.pk(slug), "SK": K.epic_sk(ekey), "type": "epic",
                "public_id": _uuid(), "key": ekey, "created_at": now,
            }
            item.update({"title": pe.title, "section": pe.section,
                         "position": pe.position, "updated_at": now})
            epic_writes.append(item)
            if existing:
                updated_epics += 1
            else:
                created_epics += 1

        # --- tasks: bulk-load existing partition, classify, batch-write ----
        existing_tasks = {
            it["key"]: it for it in self._query(
                KeyConditionExpression=Key("PK").eq(K.pk(slug))
                & Key("SK").begins_with("TASK#"),
                FilterExpression=Attr("type").eq("task"),
            ) if it.get("key")
        }
        # De-duplicate within this import by key (last wins), mirroring the old
        # read-then-write path where a later duplicate updated the earlier row.
        deduped: dict = {}
        for pt in parsed.tasks:
            try:
                validate_parsed_task(pt)
            except ValueError as exc:
                failed.append({"task_key_or_line": pt.key or pt.title or "<unknown>",
                               "error": str(exc)})
                continue
            deduped[pt.key] = pt

        task_writes = []
        for key, pt in deduped.items():
            # De-duplicate parsed tags (order-preserving) to match the Postgres
            # many-to-many, which holds each (task, tag) association only once.
            desired_tags = list(dict.fromkeys(pt.tags or []))
            existing = existing_tasks.get(key)
            if existing is None:
                pubid = _uuid()
                item = {
                    "PK": K.pk(slug), "SK": K.task_sk(pubid), "type": "task",
                    "project_slug": slug, "public_id": pubid, "key": pt.key,
                    "epic_key": pt.epic_key, "title": pt.title,
                    "description": pt.description, "status": pt.status,
                    "priority": pt.priority, "component": pt.component,
                    "proof_cmd": pt.proof_cmd, "status_note": None,
                    "section": pt.section, "owner": None, "lease_expires_at": None,
                    "position": pt.position, "version": 1, "tags": desired_tags,
                    "created_at": now, "updated_at": now, "completed_at": None,
                }
                created_tasks += 1
            elif self._task_item_unchanged(existing, pt):
                unchanged_tasks += 1  # no write, no version bump
                continue
            else:
                item = existing
                item.update({
                    "epic_key": pt.epic_key, "title": pt.title,
                    "description": pt.description, "status": pt.status,
                    "priority": pt.priority, "component": pt.component,
                    "proof_cmd": pt.proof_cmd, "section": pt.section,
                    "position": pt.position, "tags": desired_tags,
                    "updated_at": now,
                    "version": int(_pyify(item.get("version", 1))) + 1,
                })
                updated_tasks += 1
            self._apply_task_gsi(item)
            task_writes.append(item)

        with self.table.batch_writer() as bw:
            for item in epic_writes:
                bw.put_item(Item=_strip_none(_ddbify(item)))
            for item in task_writes:
                bw.put_item(Item=_strip_none(_ddbify(item)))
        return {
            "epics_created": created_epics, "epics_updated": updated_epics,
            "tasks_created": created_tasks, "tasks_updated": updated_tasks,
            "tasks_unchanged": unchanged_tasks, "failed": failed,
        }

    @staticmethod
    def _task_item_unchanged(existing: dict, pt) -> bool:
        """True when every import-controlled field of the stored task already
        matches the parsed task — mirrors the Postgres ``_task_unchanged`` so a
        re-import is a genuine no-op on both backends. Tags are compared as a set
        (association order is not meaningful) so a tag-only change IS detected and
        rewrites the task, identically to the Postgres adapter."""
        return (
            existing.get("epic_key") == pt.epic_key
            and existing.get("title") == pt.title
            and existing.get("description") == pt.description
            and existing.get("status") == pt.status
            and existing.get("priority") == pt.priority
            and existing.get("component") == pt.component
            and existing.get("proof_cmd") == pt.proof_cmd
            and existing.get("section") == pt.section
            and _pyify(existing.get("position")) == pt.position
            and set(existing.get("tags") or []) == set(pt.tags or [])
        )

    def render_spec_text(self, slug: str) -> str:
        from ..specmd import render_spec
        project = self._project_item(slug)
        epics = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("EPIC#"),
            FilterExpression=Attr("type").eq("epic"),
        )
        tasks = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("TASK#"),
            FilterExpression=Attr("type").eq("task"),
        )
        epic_views = [_View(key=e["key"], title=e["title"],
                            section=e.get("section", "backlog"),
                            position=_pyify(e.get("position", 1000.0)),
                            description=e.get("description")) for e in epics]
        task_views = [_View(
            key=t.get("key"), title=t["title"], description=t.get("description"),
            status=t["status"], priority=t.get("priority"),
            component=t.get("component"), proof_cmd=t.get("proof_cmd"),
            section=t.get("section", "backlog"),
            position=_pyify(t.get("position", 1000.0)),
            epic_key=t.get("epic_key"), tag_keys=list(t.get("tags", [])),
        ) for t in tasks]
        return render_spec(project.get("name") or slug, epic_views, task_views)

    def export_doc(self, slug: str) -> dict:
        """Full-fidelity JSON export (PORT-8): EVERY task (keyed AND keyless) with
        all core fields + tags, plus the epics. Runtime state (owner/lease/version)
        is intentionally excluded — see ``ExportDocOut``. Timestamps are returned
        as datetimes so ``ExportDocOut`` serialises them identically to Postgres."""
        project = self._project_item(slug)
        epics = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("EPIC#"),
            FilterExpression=Attr("type").eq("epic"),
        )
        tasks = self._query(
            KeyConditionExpression=Key("PK").eq(K.pk(slug))
            & Key("SK").begins_with("TASK#"),
            FilterExpression=Attr("type").eq("task"),
        )
        epics.sort(key=lambda e: (_pyify(e.get("position", 1000.0)), e.get("key") or ""))
        tasks.sort(key=lambda t: (_pyify(t.get("position", 1000.0)),
                                  t.get("created_at", ""), t["public_id"]))
        return {
            "format": _EXPORT_FORMAT,
            "project": {
                "slug": project["slug"], "name": project.get("name"),
                "description": project.get("description"),
                "default_branch": project.get("default_branch", "main"),
            },
            "epics": [{
                "public_id": e.get("public_id"), "key": e["key"],
                "title": e["title"], "description": e.get("description"),
                "section": e.get("section", "backlog"),
                "position": _pyify(e.get("position", 1000.0)),
            } for e in epics],
            "tasks": [{
                "public_id": t["public_id"], "key": t.get("key"),
                "epic_key": t.get("epic_key"), "title": t["title"],
                "description": t.get("description"), "status": t["status"],
                "priority": t.get("priority"), "component": t.get("component"),
                "proof_cmd": t.get("proof_cmd"), "status_note": t.get("status_note"),
                "section": t.get("section", "backlog"),
                "position": _pyify(t.get("position", 1000.0)),
                "tags": list(t.get("tags", [])),
                "created_at": _dt(t.get("created_at")),
                "updated_at": _dt(t.get("updated_at")),
                "completed_at": _dt(t.get("completed_at")),
            } for t in tasks],
        }

    def import_doc(self, slug: str, doc: dict) -> dict:
        """Idempotent full-fidelity JSON import (PORT-8): upsert each task by its
        stable ``public_id`` (create-with-public_id or update-existing) so KEYLESS
        tasks round-trip losslessly and re-import is a genuine no-op.

        Parity with the Postgres adapter: same dedup key (public_id), same counts,
        same skip-on-unchanged (no write, no version bump), same tag/epic handling,
        same batched write path (``batch_writer``). Runtime state (owner/lease/
        version) is NOT taken from the payload — a fresh import starts each task
        unowned at version 1."""
        from ..specmd import validate_doc_task

        self._project_item(slug)
        created_epics = updated_epics = 0
        created_tasks = updated_tasks = unchanged_tasks = 0
        failed: list[dict] = []
        now = _now_iso()

        # --- epics: bulk-load existing partition, upsert by key ------------
        existing_epics = {
            it["key"]: it for it in self._query(
                KeyConditionExpression=Key("PK").eq(K.pk(slug))
                & Key("SK").begins_with("EPIC#"),
                FilterExpression=Attr("type").eq("epic"),
            ) if it.get("key")
        }
        epic_writes = []
        for pe in doc.get("epics", []) or []:
            ekey = pe.get("key")
            if not ekey:
                continue
            existing = existing_epics.get(ekey)
            # Epics dedup on key (not public_id), so a fresh public_id is minted
            # for parity with the Postgres adapter (see the note there).
            item = existing or {
                "PK": K.pk(slug), "SK": K.epic_sk(ekey), "type": "epic",
                "public_id": _uuid(), "key": ekey,
                "created_at": _iso(pe.get("created_at")) or now,
            }
            item.update({
                "title": pe.get("title") or ekey,
                "description": pe.get("description"),
                "section": pe.get("section") or "backlog",
                "position": pe.get("position", 1000.0) if pe.get("position") is not None else 1000.0,
                "updated_at": now,
            })
            epic_writes.append(item)
            if existing:
                updated_epics += 1
            else:
                created_epics += 1

        # --- tasks: bulk-load existing partition, dedup by public_id -------
        existing_tasks = {
            it["public_id"]: it for it in self._query(
                KeyConditionExpression=Key("PK").eq(K.pk(slug))
                & Key("SK").begins_with("TASK#"),
                FilterExpression=Attr("type").eq("task"),
            )
        }
        # De-duplicate within this import by public_id (last wins), parity with
        # the Postgres adapter — otherwise two payload rows with the same
        # public_id would both write to the same SK (silent last-wins) where
        # Postgres would raise. Keyless-of-public_id rows (hand-authored) always
        # create under a minted id and are processed as-is.
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

        task_writes = []
        for t in list(deduped.values()) + keyless_rows:
            pubid = t.get("public_id") or _uuid()
            section = t.get("section") or "backlog"
            position = t.get("position", 1000.0) if t.get("position") is not None else 1000.0
            desired_tags = list(dict.fromkeys(t.get("tags") or []))
            existing = existing_tasks.get(pubid)
            if existing is None:
                item = {
                    "PK": K.pk(slug), "SK": K.task_sk(pubid), "type": "task",
                    "project_slug": slug, "public_id": pubid, "key": t.get("key"),
                    "epic_key": t.get("epic_key"), "title": t["title"],
                    "description": t.get("description"),
                    "status": t.get("status") or "todo",
                    "priority": t.get("priority"), "component": t.get("component"),
                    "proof_cmd": t.get("proof_cmd"),
                    "status_note": t.get("status_note"), "section": section,
                    "owner": None, "lease_expires_at": None, "position": position,
                    "version": 1, "tags": desired_tags,
                    "created_at": _iso(t.get("created_at")) or now,
                    "updated_at": _iso(t.get("updated_at")) or now,
                    "completed_at": _iso(t.get("completed_at")),
                }
                created_tasks += 1
            elif self._doc_item_unchanged(existing, t, section, position, desired_tags):
                unchanged_tasks += 1  # no write, no version bump
                continue
            else:
                item = existing
                item.update({
                    "key": t.get("key"), "epic_key": t.get("epic_key"),
                    "title": t["title"], "description": t.get("description"),
                    "status": t.get("status") or "todo",
                    "priority": t.get("priority"), "component": t.get("component"),
                    "proof_cmd": t.get("proof_cmd"),
                    "status_note": t.get("status_note"), "section": section,
                    "position": position, "tags": desired_tags,
                    "updated_at": now,
                    "version": int(_pyify(item.get("version", 1))) + 1,
                })
                if t.get("completed_at") is not None:
                    item["completed_at"] = _iso(t.get("completed_at"))
                updated_tasks += 1
            self._apply_task_gsi(item)
            task_writes.append(item)

        with self.table.batch_writer() as bw:
            for item in epic_writes:
                bw.put_item(Item=_strip_none(_ddbify(item)))
            for item in task_writes:
                bw.put_item(Item=_strip_none(_ddbify(item)))
        return {
            "epics_created": created_epics, "epics_updated": updated_epics,
            "tasks_created": created_tasks, "tasks_updated": updated_tasks,
            "tasks_unchanged": unchanged_tasks, "failed": failed,
        }

    @staticmethod
    def _doc_item_unchanged(existing: dict, t: dict, section: str, position,
                            desired_tags: list) -> bool:
        """True when every import-controlled field of the stored task already
        matches the JSON payload — mirrors the Postgres ``_doc_task_unchanged`` so
        a re-import is a genuine no-op on both backends. Timestamps/runtime state
        are not part of change detection (parity)."""
        return (
            existing.get("key") == t.get("key")
            and existing.get("epic_key") == t.get("epic_key")
            and existing.get("title") == t.get("title")
            and existing.get("description") == t.get("description")
            and existing.get("status") == (t.get("status") or "todo")
            and existing.get("priority") == t.get("priority")
            and existing.get("component") == t.get("component")
            and existing.get("proof_cmd") == t.get("proof_cmd")
            and existing.get("status_note") == t.get("status_note")
            and existing.get("section", "backlog") == section
            and _pyify(existing.get("position", 1000.0)) == position
            and set(existing.get("tags") or []) == set(desired_tags)
        )


# --------------------------------------------------------------------------- #
# module helpers
# --------------------------------------------------------------------------- #
def _is_conditional(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == \
        "ConditionalCheckFailedException"


def _check_version(current: int, expected: str | None) -> None:
    """Mirror of Postgres ``_check_version`` / ``helpers.check_if_match``."""
    if expected is None:
        return
    if str(current) != expected:
        raise VersionConflict(
            f"Version conflict: task is at v{current}, you sent If-Match "
            f"{expected!r}. Re-read and retry."
        )


class _View:
    """Lightweight attribute bag for the SPEC.md renderer."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
