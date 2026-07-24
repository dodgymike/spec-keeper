"""Reference storage adapter: PostgreSQL + SQLAlchemy (SLS-2).

This adapter is a *verbatim behaviour lift* of the logic that used to live in the
blueprints, ``services.py``, ``helpers.py`` and ``idempotency.py``. It keeps the
three concurrency guarantees exactly as they were:

* atomic claim   — delegates to ``services.claim_next_task`` (FOR UPDATE SKIP LOCKED)
* atomic reserve — delegates to ``services.reserve_number`` (INSERT ... ON CONFLICT)
* optimistic lock — ``_check_version`` mirrors the old ``check_if_match`` (-> 412)

Blueprints now call ``current_app.storage.<method>()`` and receive frozen DTOs.
Each mutating method owns its transaction (flush -> build DTO -> commit) so the
returned DTO carries server-populated defaults (public_id, timestamps, version).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..idempotency import lookup_idempotent, store_idempotent
from ..models import (
    Agent,
    ChainRun,
    ChainStep,
    CommitRef,
    Counter,
    Decision,
    Epic,
    EpicNote,
    Event,
    LeaseState,
    Priority,
    Project,
    ProjectMember,
    RelationKind,
    Reservation,
    Tag,
    Task,
    TaskNote,
    TaskRelation,
    TaskStatus,
    utcnow,
)
from ..services import (
    claim_next_task,
    close_active_lease,
    import_doc as _import_doc_svc,
    import_spec as _import_spec_svc,
    log_event,
    reserve_number as _reserve_number_svc,
)
from ..specmd import render_spec
from .dto import (
    AgentDTO,
    ChainRunDTO,
    ChainStepDTO,
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


# Format marker stamped on / accepted by the full-fidelity JSON document (PORT-8).
_EXPORT_FORMAT = "spec-server-full/v1"


# --------------------------------------------------------------------------- #
# ORM -> DTO converters
# --------------------------------------------------------------------------- #
def _project_dto(p: Project) -> ProjectDTO:
    return ProjectDTO(
        public_id=p.public_id, slug=p.slug, name=p.name, description=p.description,
        default_branch=p.default_branch, created_at=p.created_at, updated_at=p.updated_at,
    )


def _agent_dto(a: Agent) -> AgentDTO:
    return AgentDTO(
        public_id=a.public_id, project=a.project.slug if a.project else None,
        slug=a.slug, display_name=a.display_name, kind=a.kind, created_at=a.created_at,
    )


def _member_dto(m: ProjectMember, slug: str) -> MemberDTO:
    return MemberDTO(
        project_slug=slug, principal_sub=m.principal_sub,
        principal_name=m.principal_name, role=m.role, created_at=m.created_at,
    )


def _epic_dto(e: Epic) -> EpicDTO:
    return EpicDTO(
        public_id=e.public_id, key=e.key, title=e.title, description=e.description,
        section=e.section, position=e.position,
    )


def _commit_dto(c: CommitRef) -> CommitRefDTO:
    return CommitRefDTO(sha=c.sha, repo=c.repo, test_summary=c.test_summary,
                        created_at=c.created_at)


def _note_dto(n) -> NoteDTO:
    return NoteDTO(author=n.author, body=n.body, created_at=n.created_at)


def _task_dto(t: Task) -> TaskDTO:
    return TaskDTO(
        public_id=t.public_id, display_id=t.display_id, key=t.key,
        epic_key=t.epic.key if t.epic else None,
        title=t.title, description=t.description, status=t.status, priority=t.priority,
        component=t.component, proof_cmd=t.proof_cmd, status_note=t.status_note,
        section=t.section, owner=t.owner, lease_expires_at=t.lease_expires_at,
        position=t.position, version=t.version,
        tags=[tag.key for tag in t.tags],
        commits=[_commit_dto(c) for c in t.commits],
        notes=[_note_dto(n) for n in t.notes],
        created_at=t.created_at, updated_at=t.updated_at, completed_at=t.completed_at,
    )


def _reservation_dto(r: Reservation) -> ReservationDTO:
    return ReservationDTO(namespace=r.namespace, value=r.value,
                          reserved_by=r.reserved_by, note=r.note, created_at=r.created_at)


def _counter_dto(c: Counter) -> CounterDTO:
    return CounterDTO(namespace=c.namespace, current_value=c.current_value)


def _event_dto(e: Event) -> EventDTO:
    return EventDTO(event_type=e.event_type, agent=e.agent,
                    task_pubid=e.task.public_id if e.task else None,
                    message=e.message, payload=e.payload, created_at=e.created_at)


def _decision_dto(d: Decision) -> DecisionDTO:
    return DecisionDTO(public_id=d.public_id, key=d.key, title=d.title,
                       decision=d.decision, context=d.context,
                       consequences=d.consequences, agent=d.agent, created_at=d.created_at)


def _chain_step_dto(s: ChainStep) -> ChainStepDTO:
    return ChainStepDTO(step_name=s.step_name, step_order=s.step_order, agent=s.agent,
                        status=s.status, skip_justification=s.skip_justification,
                        output_ref=s.output_ref)


def _chain_run_dto(r: ChainRun) -> ChainRunDTO:
    return ChainRunDTO(public_id=r.public_id, status=r.status, started_by=r.started_by,
                       started_at=r.started_at, finished_at=r.finished_at,
                       steps=[_chain_step_dto(s) for s in r.steps])


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class PostgresBackend:
    """The reference ``StorageBackend`` over Flask-SQLAlchemy's ``db.session``."""

    # ----- health --------------------------------------------------------
    def ping(self) -> None:
        """Cheap liveness check for /readyz: the database answers ``SELECT 1``.

        Returns ``None`` on success; on failure rolls back the (now-poisoned)
        session and raises the neutral ``BackendUnavailable``."""
        try:
            db.session.execute(sa.text("SELECT 1"))
        except Exception as exc:
            db.session.rollback()
            raise BackendUnavailable(str(exc)) from exc

    # ----- internal lookups (raise backend-neutral errors) -------------
    def _project(self, slug: str) -> Project:
        project = db.session.execute(
            sa.select(Project).where(Project.slug == slug)
        ).scalar_one_or_none()
        if project is None:
            raise NotFound(f"Project '{slug}' not found.")
        return project

    def _epic(self, project_id: int, key: str) -> Epic:
        epic = db.session.execute(
            sa.select(Epic).where(Epic.project_id == project_id, Epic.key == key)
        ).scalar_one_or_none()
        if epic is None:
            raise NotFound(f"Epic '{key}' not found.")
        return epic

    def _task(self, project_id: int, ident: str) -> Task:
        task = db.session.execute(
            sa.select(Task).where(Task.project_id == project_id, Task.key == ident)
        ).scalar_one_or_none()
        if task is None:
            task = db.session.execute(
                sa.select(Task).where(
                    Task.project_id == project_id, Task.public_id == ident
                )
            ).scalar_one_or_none()
        if task is None:
            raise NotFound(f"Task '{ident}' not found.")
        return task

    @staticmethod
    def _check_version(task: Task, expected: str | None) -> None:
        """Mirror of the old ``helpers.check_if_match``: a parsed If-Match token
        (quotes/leading ``v`` already stripped) must equal the current version."""
        if expected is None:
            return
        if str(task.version) != expected:
            raise VersionConflict(
                f"Version conflict: task is at v{task.version}, you sent "
                f"If-Match {expected!r}. Re-read and retry."
            )

    def _get_or_create_tag(self, project_id: int, key: str) -> Tag:
        tag = db.session.execute(
            sa.select(Tag).where(Tag.project_id == project_id, Tag.key == key)
        ).scalar_one_or_none()
        if tag is None:
            tag = Tag(project_id=project_id, key=key)
            db.session.add(tag)
            db.session.flush()
        return tag

    # ----- projects ----------------------------------------------------
    def list_projects(self) -> list[ProjectDTO]:
        rows = db.session.execute(
            sa.select(Project).order_by(Project.slug)
        ).scalars().all()
        return [_project_dto(p) for p in rows]

    def get_project(self, slug: str) -> ProjectDTO:
        return _project_dto(self._project(slug))

    def create_project(self, data: dict, *, creator_sub: str | None = None,
                       creator_name: str | None = None) -> ProjectDTO:
        existing = db.session.execute(
            sa.select(Project).where(Project.slug == data["slug"])
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(f"Project '{data['slug']}' already exists.")
        project = Project(**data)
        db.session.add(project)
        # Backend parity (ISO-8): the pre-check above is racy — a concurrent create
        # can slip a duplicate slug past it, and the UNIQUE(projects.slug) constraint
        # then trips as an IntegrityError. Postgres enforces it immediately, so the
        # violation can surface either on the flush (INSERT ... RETURNING) or on the
        # commit; guard the whole write. Catch it, roll back the entire transaction
        # (project row AND any creator-admin member, so no partial state survives),
        # and re-raise the SAME Conflict the DynamoDB adapter raises -> identical 409
        # on both backends.
        try:
            db.session.flush()
            # Creator-auto-admin (ISO-4): stamp the verified creator as an ``admin``
            # member in the SAME transaction as the project row — a project must never
            # exist without an admin member (that is a lockout). Skipped when there is
            # no authenticated identity (local/auth-off, ``creator_sub`` is None).
            if creator_sub:
                db.session.add(ProjectMember(
                    project_id=project.id, principal_sub=creator_sub,
                    principal_name=creator_name, role="admin",
                ))
            dto = _project_dto(project)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise Conflict(f"Project '{data['slug']}' already exists.") from exc
        return dto

    def update_project(self, slug: str, patch: dict) -> ProjectDTO:
        project = self._project(slug)
        for k, v in patch.items():
            setattr(project, k, v)
        db.session.flush()
        dto = _project_dto(project)
        db.session.commit()
        return dto

    def delete_project(self, slug: str) -> None:
        project = self._project(slug)
        db.session.delete(project)
        db.session.commit()

    # ----- agents ------------------------------------------------------
    def list_agents(self, slug: str) -> list[AgentDTO]:
        project = self._project(slug)
        rows = db.session.execute(
            sa.select(Agent).where(Agent.project_id == project.id).order_by(Agent.slug)
        ).scalars().all()
        return [_agent_dto(a) for a in rows]

    def upsert_agent(self, slug: str, data: dict) -> AgentDTO:
        project = self._project(slug)
        agent = db.session.execute(
            sa.select(Agent).where(
                Agent.project_id == project.id, Agent.slug == data["slug"]
            )
        ).scalar_one_or_none()
        if agent is None:
            agent = Agent(project_id=project.id, **data)
            db.session.add(agent)
        else:
            for k, v in data.items():
                setattr(agent, k, v)
        db.session.flush()
        dto = _agent_dto(agent)
        db.session.commit()
        return dto

    # ----- project membership (ISO-1; dormant) -------------------------
    def get_membership(self, project_slug: str, principal_sub: str) -> MemberDTO | None:
        project = self._project(project_slug)
        member = db.session.execute(
            sa.select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.principal_sub == principal_sub,
            )
        ).scalar_one_or_none()
        return _member_dto(member, project_slug) if member is not None else None

    def list_members(self, project_slug: str) -> list[MemberDTO]:
        project = self._project(project_slug)
        rows = db.session.execute(
            sa.select(ProjectMember)
            .where(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.principal_sub)
        ).scalars().all()
        return [_member_dto(m, project_slug) for m in rows]

    def add_member(self, project_slug: str, principal_sub: str,
                   principal_name: str | None, role: str) -> MemberDTO:
        project = self._project(project_slug)
        member = db.session.execute(
            sa.select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.principal_sub == principal_sub,
            )
        ).scalar_one_or_none()
        if member is None:
            member = ProjectMember(
                project_id=project.id, principal_sub=principal_sub,
                principal_name=principal_name, role=role,
            )
            db.session.add(member)
        else:
            member.principal_name = principal_name
            member.role = role
        db.session.flush()
        dto = _member_dto(member, project_slug)
        db.session.commit()
        return dto

    def remove_member(self, project_slug: str, principal_sub: str) -> None:
        project = self._project(project_slug)
        member = db.session.execute(
            sa.select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.principal_sub == principal_sub,
            )
        ).scalar_one_or_none()
        if member is not None:
            db.session.delete(member)
            db.session.commit()

    def list_projects_for_principal(self, principal_sub: str) -> list[MemberDTO]:
        rows = db.session.execute(
            sa.select(ProjectMember, Project.slug)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.principal_sub == principal_sub)
            .order_by(Project.slug)
        ).all()
        return [_member_dto(m, slug) for m, slug in rows]

    # ----- epics -------------------------------------------------------
    def list_epics(self, slug: str) -> list[EpicDTO]:
        project = self._project(slug)
        rows = db.session.execute(
            sa.select(Epic).where(Epic.project_id == project.id)
            .order_by(Epic.position, Epic.key)
        ).scalars().all()
        return [_epic_dto(e) for e in rows]

    def create_epic(self, slug: str, data: dict) -> EpicDTO:
        project = self._project(slug)
        existing = db.session.execute(
            sa.select(Epic).where(
                Epic.project_id == project.id, Epic.key == data["key"]
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(f"Epic '{data['key']}' already exists.")
        epic = Epic(project_id=project.id, **data)
        db.session.add(epic)
        db.session.flush()
        dto = _epic_dto(epic)
        db.session.commit()
        return dto

    def get_epic(self, slug: str, key: str) -> EpicDTO:
        project = self._project(slug)
        return _epic_dto(self._epic(project.id, key))

    def update_epic(self, slug: str, key: str, patch: dict) -> EpicDTO:
        project = self._project(slug)
        epic = self._epic(project.id, key)
        for k, v in patch.items():
            setattr(epic, k, v)
        db.session.flush()
        dto = _epic_dto(epic)
        db.session.commit()
        return dto

    def list_epic_notes(self, slug: str, key: str) -> list[NoteDTO]:
        project = self._project(slug)
        epic = self._epic(project.id, key)
        return [_note_dto(n) for n in epic.notes]

    def append_epic_note(self, slug: str, key: str, data: dict) -> NoteDTO:
        project = self._project(slug)
        epic = self._epic(project.id, key)
        note = EpicNote(epic_id=epic.id, body=data["body"], author=data.get("author"))
        db.session.add(note)
        log_event(project.id, "note", agent=data.get("author"),
                  message=f"note on epic {epic.key}: {data['body'][:120]}")
        db.session.flush()
        dto = _note_dto(note)
        db.session.commit()
        return dto

    # ----- tasks: CRUD -------------------------------------------------
    def list_tasks(self, slug: str, flt: dict) -> list[TaskDTO]:
        project = self._project(slug)
        query = sa.select(Task).where(Task.project_id == project.id)
        if "status" in flt:
            query = query.where(Task.status == TaskStatus(flt["status"]))
        if "owner" in flt:
            query = query.where(Task.owner == flt["owner"])
        if "priority" in flt:
            query = query.where(Task.priority == Priority(flt["priority"]))
        if "epic" in flt:
            epic = self._epic(project.id, flt["epic"])
            query = query.where(Task.epic_id == epic.id)
        if "tag" in flt:
            query = query.where(Task.tags.any(Tag.key == flt["tag"]))
        if "q" in flt:
            like = f"%{flt['q']}%"
            query = query.where(
                sa.or_(Task.title.ilike(like), Task.description.ilike(like))
            )
        query = (
            query.order_by(Task.position, Task.id)
            .offset(flt["offset"]).limit(flt["limit"])
        )
        rows = db.session.execute(query).scalars().all()
        return [_task_dto(t) for t in rows]

    def create_task(self, slug: str, data: dict) -> TaskDTO:
        project = self._project(slug)
        data = dict(data)
        tags = data.pop("tags", [])
        epic_key = data.pop("epic_key", None)
        epic_id = None
        if epic_key:
            epic_id = self._epic(project.id, epic_key).id
        if data.get("key"):
            dup = db.session.execute(
                sa.select(Task).where(
                    Task.project_id == project.id, Task.key == data["key"]
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise Conflict(f"Task key '{data['key']}' already exists.")
        if data.get("status"):
            data["status"] = TaskStatus(data["status"])
        if data.get("priority"):
            data["priority"] = Priority(data["priority"])
        task = Task(project_id=project.id, epic_id=epic_id, **data)
        for key in tags:
            task.tags.append(self._get_or_create_tag(project.id, key))
        db.session.add(task)
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def get_task(self, slug: str, ident: str) -> TaskDTO:
        project = self._project(slug)
        return _task_dto(self._task(project.id, ident))

    def update_task(self, slug: str, ident: str, patch: dict,
                    expected_version: str | None) -> TaskDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        self._check_version(task, expected_version)
        data = dict(patch)
        if "epic_key" in data:
            ek = data.pop("epic_key")
            task.epic_id = self._epic(project.id, ek).id if ek else None
        if "status" in data:
            data["status"] = TaskStatus(data["status"])
        if "priority" in data and data["priority"]:
            data["priority"] = Priority(data["priority"])
        for k, v in data.items():
            setattr(task, k, v)
        task.version += 1
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def delete_task(self, slug: str, ident: str) -> None:
        project = self._project(slug)
        task = self._task(project.id, ident)
        db.session.delete(task)
        db.session.commit()

    # ----- tasks: atomic guarantees + lifecycle ------------------------
    def claim_next(self, slug: str, agent: str, *, epic=None, priority_max=None,
                   component=None, lease_ttl=None, idempotency_key=None,
                   serialize=None) -> IdempotentOutcome:
        project = self._project(slug)
        if idempotency_key:
            existing = lookup_idempotent(project.id, "claim-next", idempotency_key)
            if existing is not None:
                return IdempotentOutcome(replay_body=existing.response_json,
                                         replay_status=existing.status_code)
        epic_id = None
        if epic:
            epic_id = self._epic(project.id, epic).id
        pmax = Priority(priority_max) if priority_max else None
        task = claim_next_task(
            project_id=project.id, agent=agent, epic_id=epic_id,
            priority_max=pmax, component=component, lease_ttl=lease_ttl,
        )
        if task is None:
            db.session.commit()
            return IdempotentOutcome()
        dto = _task_dto(task)
        if idempotency_key and serialize is not None:
            store_idempotent(project.id, "claim-next", idempotency_key,
                             serialize(dto), 200)
        db.session.commit()
        return IdempotentOutcome(result=dto)

    def complete_task(self, slug: str, ident: str, data: dict,
                      expected_version: str | None) -> TaskDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        self._check_version(task, expected_version)
        task.status = TaskStatus.done
        task.completed_at = utcnow()
        task.lease_expires_at = None
        task.owner = None
        if data.get("proof_cmd"):
            task.proof_cmd = data["proof_cmd"]
        task.version += 1
        if data.get("commit_sha"):
            db.session.add(CommitRef(
                task_id=task.id, sha=data["commit_sha"],
                repo=data.get("repo"), test_summary=data.get("test_summary"),
            ))
        close_active_lease(task.id, LeaseState.completed)
        log_event(project.id, "completed", task_id=task.id,
                  message=f"completed {task.display_id}",
                  payload={k: v for k, v in data.items() if v})
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def release_task(self, slug: str, ident: str, reset_to: str) -> TaskDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        task.status = TaskStatus(reset_to)
        task.owner = None
        task.lease_expires_at = None
        task.version += 1
        close_active_lease(task.id, LeaseState.released)
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def set_status(self, slug: str, ident: str, status: str, note, has_note: bool,
                   expected_version: str | None) -> TaskDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        self._check_version(task, expected_version)
        task.status = TaskStatus(status)
        if has_note:
            task.status_note = note
        if task.status == TaskStatus.done:
            task.completed_at = utcnow()
        task.version += 1
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def add_commit(self, slug: str, ident: str, data: dict) -> TaskDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        exists = db.session.execute(
            sa.select(CommitRef).where(
                CommitRef.task_id == task.id, CommitRef.sha == data["sha"]
            )
        ).scalar_one_or_none()
        if exists is None:
            db.session.add(CommitRef(task_id=task.id, **data))
        db.session.flush()
        dto = _task_dto(task)
        db.session.commit()
        return dto

    def list_task_notes(self, slug: str, ident: str) -> list[NoteDTO]:
        project = self._project(slug)
        task = self._task(project.id, ident)
        return [_note_dto(n) for n in task.notes]

    def append_task_note(self, slug: str, ident: str, data: dict) -> NoteDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        note = TaskNote(task_id=task.id, body=data["body"], author=data.get("author"))
        db.session.add(note)
        log_event(project.id, "note", agent=data.get("author"), task_id=task.id,
                  message=f"note on {task.display_id}: {data['body'][:120]}")
        db.session.flush()
        dto = _note_dto(note)
        db.session.commit()
        return dto

    def add_relation(self, slug: str, ident: str, target: str, kind: str) -> str:
        project = self._project(slug)
        src = self._task(project.id, ident)
        dst = self._task(project.id, target)
        kind_enum = RelationKind(kind)
        exists = db.session.execute(
            sa.select(TaskRelation).where(
                TaskRelation.src_task_id == src.id,
                TaskRelation.dst_task_id == dst.id,
                TaskRelation.kind == kind_enum,
            )
        ).scalar_one_or_none()
        if exists is None:
            db.session.add(TaskRelation(
                src_task_id=src.id, dst_task_id=dst.id, kind=kind_enum
            ))
            if kind_enum == RelationKind.supersedes:
                dst.status = TaskStatus.superseded
                dst.superseded_by_task_id = src.id
        message = f"{src.display_id} {kind_enum.value} {dst.display_id}"
        db.session.commit()
        return message

    # ----- reservations / counters -------------------------------------
    def reserve_number(self, slug: str, namespace: str, *, reserved_by=None,
                       task_key=None, note=None, idempotency_key=None,
                       serialize=None) -> IdempotentOutcome:
        project = self._project(slug)
        if idempotency_key:
            existing = lookup_idempotent(project.id, "reserve", idempotency_key)
            if existing is not None:
                return IdempotentOutcome(replay_body=existing.response_json,
                                         replay_status=existing.status_code)
        task_id = None
        if task_key:
            task_id = self._task(project.id, task_key).id
        reservation = _reserve_number_svc(
            project_id=project.id, namespace=namespace,
            reserved_by=reserved_by, task_id=task_id, note=note,
        )
        dto = _reservation_dto(reservation)
        if idempotency_key and serialize is not None:
            store_idempotent(project.id, "reserve", idempotency_key,
                             serialize(dto), 201)
        db.session.commit()
        return IdempotentOutcome(result=dto)

    def list_reservations(self, slug: str, namespace) -> list[ReservationDTO]:
        project = self._project(slug)
        query = sa.select(Reservation).where(Reservation.project_id == project.id)
        if namespace:
            query = query.where(Reservation.namespace == namespace)
        rows = db.session.execute(
            query.order_by(Reservation.namespace, Reservation.value)
        ).scalars().all()
        return [_reservation_dto(r) for r in rows]

    def list_counters(self, slug: str) -> list[CounterDTO]:
        project = self._project(slug)
        rows = db.session.execute(
            sa.select(Counter).where(Counter.project_id == project.id)
            .order_by(Counter.namespace)
        ).scalars().all()
        return [_counter_dto(c) for c in rows]

    # ----- events / notes-feed / decisions -----------------------------
    def create_event(self, slug: str, data: dict) -> EventDTO:
        project = self._project(slug)
        task_id = None
        if data.get("task_key"):
            task_id = self._task(project.id, data["task_key"]).id
        event = log_event(
            project.id, data["event_type"], agent=data.get("agent"),
            task_id=task_id, message=data.get("message"),
            payload=data.get("payload") or {},
        )
        db.session.flush()
        dto = _event_dto(event)
        db.session.commit()
        return dto

    def list_events(self, slug: str, flt: dict) -> list[EventDTO]:
        project = self._project(slug)
        query = sa.select(Event).where(Event.project_id == project.id)
        if "event_type" in flt:
            query = query.where(Event.event_type == flt["event_type"])
        if "agent" in flt:
            query = query.where(Event.agent == flt["agent"])
        if "task" in flt:
            task = self._task(project.id, flt["task"])
            query = query.where(Event.task_id == task.id)
        query = (
            query.order_by(Event.created_at.desc(), Event.id.desc())
            .offset(flt["offset"]).limit(flt["limit"])
        )
        rows = db.session.execute(query).scalars().all()
        return [_event_dto(e) for e in rows]

    def list_project_notes(self, slug: str, flt: dict) -> list[ProjectNoteDTO]:
        project = self._project(slug)
        scope = flt["scope"]
        cap = flt["offset"] + flt["limit"]
        rows: list[ProjectNoteDTO] = []

        want_task = scope in ("task", "all") and "epic" not in flt
        want_epic = scope in ("epic", "all") and "task" not in flt

        if want_task:
            q = (
                sa.select(TaskNote, Task.key, Task.public_id)
                .join(Task, Task.id == TaskNote.task_id)
                .where(Task.project_id == project.id)
            )
            if "author" in flt:
                q = q.where(TaskNote.author == flt["author"])
            if "task" in flt:
                t = self._task(project.id, flt["task"])
                q = q.where(TaskNote.task_id == t.id)
            if "since" in flt:
                q = q.where(TaskNote.created_at >= flt["since"])
            q = q.order_by(TaskNote.created_at.desc(), TaskNote.id.desc()).limit(cap)
            for n, key, pub in db.session.execute(q):
                rows.append(ProjectNoteDTO(
                    scope="task", task=key or pub, epic=None,
                    author=n.author, body=n.body, created_at=n.created_at,
                ))

        if want_epic:
            q = (
                sa.select(EpicNote, Epic.key)
                .join(Epic, Epic.id == EpicNote.epic_id)
                .where(Epic.project_id == project.id)
            )
            if "author" in flt:
                q = q.where(EpicNote.author == flt["author"])
            if "epic" in flt:
                q = q.where(Epic.key == flt["epic"])
            if "since" in flt:
                q = q.where(EpicNote.created_at >= flt["since"])
            q = q.order_by(EpicNote.created_at.desc(), EpicNote.id.desc()).limit(cap)
            for n, key in db.session.execute(q):
                rows.append(ProjectNoteDTO(
                    scope="epic", task=None, epic=key,
                    author=n.author, body=n.body, created_at=n.created_at,
                ))

        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[flt["offset"]: flt["offset"] + flt["limit"]]

    def list_decisions(self, slug: str) -> list[DecisionDTO]:
        project = self._project(slug)
        rows = db.session.execute(
            sa.select(Decision).where(Decision.project_id == project.id)
            .order_by(Decision.created_at.desc(), Decision.id.desc())
        ).scalars().all()
        return [_decision_dto(d) for d in rows]

    def create_decision(self, slug: str, data: dict) -> DecisionDTO:
        project = self._project(slug)
        data = dict(data)
        task_id = None
        if data.get("task_key"):
            task_id = self._task(project.id, data.pop("task_key")).id
        else:
            data.pop("task_key", None)
        decision = Decision(project_id=project.id, task_id=task_id, **data)
        db.session.add(decision)
        log_event(project.id, "decision", agent=data.get("agent"), task_id=task_id,
                  message=f"decision: {data['title']}")
        db.session.flush()
        dto = _decision_dto(decision)
        db.session.commit()
        return dto

    # ----- chains ------------------------------------------------------
    def _chain_run(self, project_id: int, run_pubid: str) -> ChainRun:
        run = db.session.execute(
            sa.select(ChainRun).where(
                ChainRun.project_id == project_id, ChainRun.public_id == run_pubid
            )
        ).scalar_one_or_none()
        if run is None:
            raise NotFound(f"Chain run '{run_pubid}' not found.")
        return run

    def create_chain_run(self, slug: str, ident: str, started_by) -> ChainRunDTO:
        project = self._project(slug)
        task = self._task(project.id, ident)
        run = ChainRun(project_id=project.id, task_id=task.id,
                       started_by=started_by, status="running")
        db.session.add(run)
        db.session.flush()
        log_event(project.id, "chain_run", agent=started_by, task_id=task.id,
                  message=f"chain run started for {task.display_id}",
                  payload={"run": run.public_id})
        db.session.flush()
        dto = _chain_run_dto(run)
        db.session.commit()
        return dto

    def list_chain_runs(self, slug: str, task_ident=None, *, limit=200,
                        offset=0) -> list[ChainRunDTO]:
        project = self._project(slug)
        query = sa.select(ChainRun).where(ChainRun.project_id == project.id)
        if task_ident is not None:
            task = self._task(project.id, task_ident)
            query = query.where(ChainRun.task_id == task.id)
        query = (
            query.order_by(ChainRun.started_at.desc(), ChainRun.id.desc())
            .offset(offset).limit(limit)
        )
        rows = db.session.execute(query).scalars().all()
        return [_chain_run_dto(r) for r in rows]

    def get_chain_run(self, slug: str, run_pubid: str) -> ChainRunDTO:
        project = self._project(slug)
        return _chain_run_dto(self._chain_run(project.id, run_pubid))

    def update_chain_run(self, slug: str, run_pubid: str, status) -> ChainRunDTO:
        project = self._project(slug)
        run = self._chain_run(project.id, run_pubid)
        if status is not None:
            run.status = status
            if status in ("passed", "failed", "aborted"):
                run.finished_at = utcnow()
        db.session.flush()
        dto = _chain_run_dto(run)
        db.session.commit()
        return dto

    def upsert_chain_step(self, slug: str, run_pubid: str, step_name: str,
                          data: dict) -> ChainStepDTO:
        project = self._project(slug)
        run = self._chain_run(project.id, run_pubid)
        step = db.session.execute(
            sa.select(ChainStep).where(
                ChainStep.run_id == run.id, ChainStep.step_name == step_name
            )
        ).scalar_one_or_none()
        if step is None:
            step = ChainStep(run_id=run.id, step_name=step_name)
            db.session.add(step)
        step.step_order = data["step_order"]
        step.agent = data.get("agent")
        step.status = data["status"]
        step.skip_justification = data.get("skip_justification")
        step.output_ref = data.get("output_ref")
        db.session.flush()
        log_event(project.id, "chain_step", agent=data.get("agent"),
                  task_id=run.task_id,
                  message=f"chain step {step_name} -> {data['status']}",
                  payload={"run": run.public_id, "step": step_name,
                           "status": data["status"]})
        db.session.flush()
        dto = _chain_step_dto(step)
        db.session.commit()
        return dto

    # ----- ports -------------------------------------------------------
    def import_spec(self, slug: str, parsed) -> dict:
        project = self._project(slug)
        counts = _import_spec_svc(project.id, parsed)
        db.session.commit()
        return counts

    def render_spec_text(self, slug: str) -> str:
        project = self._project(slug)
        epics = db.session.execute(
            sa.select(Epic).where(Epic.project_id == project.id)
        ).scalars().all()
        epic_key_by_id = {e.id: e.key for e in epics}
        tasks = db.session.execute(
            sa.select(Task).where(Task.project_id == project.id)
        ).scalars().all()
        render_tasks = [_RenderTask(t, epic_key_by_id.get(t.epic_id)) for t in tasks]
        return render_spec(project.name or project.slug, epics, render_tasks)

    def export_doc(self, slug: str) -> dict:
        """Full-fidelity JSON export (PORT-8): EVERY task (keyed AND keyless) with
        all core fields + tags, plus the epics. Runtime state (owner/lease/version)
        is intentionally excluded — see ``ExportDocOut``."""
        project = self._project(slug)
        epics = db.session.execute(
            sa.select(Epic)
            .where(Epic.project_id == project.id)
            .order_by(Epic.position, Epic.id)
        ).scalars().all()
        epic_key_by_id = {e.id: e.key for e in epics}
        tasks = db.session.execute(
            sa.select(Task)
            .where(Task.project_id == project.id)
            .options(selectinload(Task.tags))
            .order_by(Task.position, Task.id)
        ).scalars().all()
        return {
            "format": _EXPORT_FORMAT,
            "project": {
                "slug": project.slug, "name": project.name,
                "description": project.description,
                "default_branch": project.default_branch,
            },
            "epics": [{
                "public_id": e.public_id, "key": e.key, "title": e.title,
                "description": e.description, "section": e.section,
                "position": e.position,
            } for e in epics],
            "tasks": [{
                "public_id": t.public_id, "key": t.key,
                "epic_key": epic_key_by_id.get(t.epic_id),
                "title": t.title, "description": t.description,
                "status": t.status.value,
                "priority": t.priority.value if t.priority else None,
                "component": t.component, "proof_cmd": t.proof_cmd,
                "status_note": t.status_note, "section": t.section,
                "position": t.position, "tags": [tag.key for tag in t.tags],
                "created_at": t.created_at, "updated_at": t.updated_at,
                "completed_at": t.completed_at,
            } for t in tasks],
        }

    def import_doc(self, slug: str, doc: dict) -> dict:
        """Idempotent full-fidelity JSON import (PORT-8): upsert each task by its
        stable ``public_id`` (create-with-public_id or update-existing) so KEYLESS
        tasks round-trip losslessly and re-import is a genuine no-op."""
        project = self._project(slug)
        counts = _import_doc_svc(project.id, doc)
        db.session.commit()
        return counts


class _RenderTask:
    """Lightweight view object the SPEC.md renderer understands."""

    def __init__(self, t: Task, epic_key):
        self.key = t.key
        self.title = t.title
        self.description = t.description
        self.status = t.status.value
        self.priority = t.priority.value if t.priority else None
        self.component = t.component
        self.proof_cmd = t.proof_cmd
        self.section = t.section
        self.position = t.position
        self.epic_key = epic_key
        self.tag_keys = [tag.key for tag in t.tags]
