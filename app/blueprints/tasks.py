"""Tasks: CRUD plus the concurrency-critical claim/complete/reserve flows."""
from __future__ import annotations

import sqlalchemy as sa
from flask import Response, jsonify, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..extensions import db
from ..idempotency import (
    idempotency_key_from_request,
    lookup_idempotent,
    store_idempotent,
)
from ..helpers import (
    check_if_match,
    etag_headers,
    get_epic_or_404,
    get_project_or_404,
    get_task_or_404,
    require_api_key,
)
from ..models import (
    CommitRef,
    LeaseState,
    Priority,
    RelationKind,
    Tag,
    Task,
    TaskNote,
    TaskRelation,
    TaskStatus,
    utcnow,
)
from ..schemas import (
    ClaimNextIn,
    CommitIn,
    CompleteIn,
    MessageOut,
    NoteIn,
    NoteOut,
    RelationIn,
    ReleaseIn,
    StatusIn,
    TaskIn,
    TaskOut,
    TaskPatch,
    TaskQuery,
)
from ..jira_sync import sync_task_completed, sync_task_created
from ..services import claim_next_task, close_active_lease, log_event

blp = Blueprint(
    "tasks", __name__, url_prefix="/api/v1/projects/<slug>/tasks",
    description="The task backlog. Atomically claim, complete and assign tasks.",
)


def _get_or_create_tag(project_id: int, key: str) -> Tag:
    tag = db.session.execute(
        sa.select(Tag).where(Tag.project_id == project_id, Tag.key == key)
    ).scalar_one_or_none()
    if tag is None:
        tag = Tag(project_id=project_id, key=key)
        db.session.add(tag)
        db.session.flush()
    return tag


@blp.route("")
class TasksCollection(MethodView):
    @blp.arguments(TaskQuery, location="query")
    @blp.response(200, TaskOut(many=True))
    def get(self, args, slug):
        """List/filter tasks. Filter by ``owner`` to see one agent's specs."""
        require_api_key()
        project = get_project_or_404(slug)
        query = sa.select(Task).where(Task.project_id == project.id)

        if "status" in args:
            query = query.where(Task.status == TaskStatus(args["status"]))
        if "owner" in args:
            query = query.where(Task.owner == args["owner"])
        if "priority" in args:
            query = query.where(Task.priority == Priority(args["priority"]))
        if "epic" in args:
            epic = get_epic_or_404(project.id, args["epic"])
            query = query.where(Task.epic_id == epic.id)
        if "tag" in args:
            query = query.where(Task.tags.any(Tag.key == args["tag"]))
        if "q" in args:
            like = f"%{args['q']}%"
            query = query.where(
                sa.or_(Task.title.ilike(like), Task.description.ilike(like))
            )

        query = (
            query.order_by(Task.position, Task.id)
            .offset(args["offset"])
            .limit(args["limit"])
        )
        return db.session.execute(query).scalars().all()

    @blp.arguments(TaskIn)
    @blp.response(201, TaskOut)
    def post(self, data, slug):
        """Create a task."""
        require_api_key()
        project = get_project_or_404(slug)
        tags = data.pop("tags", [])
        epic_key = data.pop("epic_key", None)
        epic_id = None
        if epic_key:
            epic_id = get_epic_or_404(project.id, epic_key).id

        if data.get("key"):
            dup = db.session.execute(
                sa.select(Task).where(
                    Task.project_id == project.id, Task.key == data["key"]
                )
            ).scalar_one_or_none()
            if dup is not None:
                abort(409, message=f"Task key '{data['key']}' already exists.")

        if data.get("status"):
            data["status"] = TaskStatus(data["status"])
        if data.get("priority"):
            data["priority"] = Priority(data["priority"])

        task = Task(project_id=project.id, epic_id=epic_id, **data)
        for key in tags:
            task.tags.append(_get_or_create_tag(project.id, key))
        db.session.add(task)
        db.session.commit()
        sync_task_created(task)
        return task


@blp.route("/claim-next")
class ClaimNext(MethodView):
    @blp.arguments(ClaimNextIn)
    @blp.response(200, TaskOut)
    @blp.alt_response(204, description="No claimable task available.")
    def post(self, data, slug):
        """Atomically claim the next todo task (FOR UPDATE SKIP LOCKED).

        Two agents calling at the same time never receive the same task.
        Returns 204 when nothing is claimable."""
        require_api_key()
        project = get_project_or_404(slug)

        idem_key = idempotency_key_from_request()
        if idem_key:
            existing = lookup_idempotent(project.id, "claim-next", idem_key)
            if existing is not None:
                return jsonify(existing.response_json), existing.status_code

        epic_id = None
        if data.get("epic"):
            epic_id = get_epic_or_404(project.id, data["epic"]).id
        priority_max = Priority(data["priority_max"]) if data.get("priority_max") else None

        task = claim_next_task(
            project_id=project.id,
            agent=data["agent"],
            epic_id=epic_id,
            priority_max=priority_max,
            component=data.get("component"),
            lease_ttl=data.get("lease_ttl"),
        )
        if task is None:
            db.session.commit()
            return Response(status=204)
        if idem_key:
            store_idempotent(
                project.id, "claim-next", idem_key, TaskOut().dump(task), 200
            )
        db.session.commit()
        return task, 200, etag_headers(task)


@blp.route("/<ident>")
class TaskItem(MethodView):
    @blp.response(200, TaskOut)
    def get(self, slug, ident):
        """Get a task by key or public_id. Returns an ETag for optimistic locking."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        return task, 200, etag_headers(task)

    @blp.arguments(TaskPatch)
    @blp.response(200, TaskOut)
    def patch(self, data, slug, ident):
        """Update task fields. Honours ``If-Match`` (412 on version conflict)."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        check_if_match(task)

        if "epic_key" in data:
            ek = data.pop("epic_key")
            task.epic_id = get_epic_or_404(project.id, ek).id if ek else None
        if "status" in data:
            data["status"] = TaskStatus(data["status"])
        if "priority" in data and data["priority"]:
            data["priority"] = Priority(data["priority"])
        for k, v in data.items():
            setattr(task, k, v)
        task.version += 1
        db.session.commit()
        return task, 200, etag_headers(task)

    @blp.response(204)
    def delete(self, slug, ident):
        """Delete a task."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        db.session.delete(task)
        db.session.commit()
        return ""


@blp.route("/<ident>/complete")
class TaskComplete(MethodView):
    @blp.arguments(CompleteIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Complete a task: flip to done, close its lease, record commit/proof.

        This is the spec-keeper 'flip the checkbox to [x]' operation."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        check_if_match(task)

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
        db.session.commit()
        sync_task_completed(task)
        return task, 200, etag_headers(task)


@blp.route("/<ident>/release")
class TaskRelease(MethodView):
    @blp.arguments(ReleaseIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Release a claimed task without completing it (back to todo)."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        task.status = TaskStatus(data["reset_to"])
        task.owner = None
        task.lease_expires_at = None
        task.version += 1
        close_active_lease(task.id, LeaseState.released)
        db.session.commit()
        return task, 200, etag_headers(task)


@blp.route("/<ident>/status")
class TaskStatusUpdate(MethodView):
    @blp.arguments(StatusIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Set an explicit status (blocked/deferred/superseded/...) with a note."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        check_if_match(task)
        task.status = TaskStatus(data["status"])
        if "note" in data:
            task.status_note = data["note"]
        if task.status == TaskStatus.done:
            task.completed_at = utcnow()
        task.version += 1
        db.session.commit()
        return task, 200, etag_headers(task)


@blp.route("/<ident>/commits")
class TaskCommits(MethodView):
    @blp.arguments(CommitIn)
    @blp.response(201, TaskOut)
    def post(self, data, slug, ident):
        """Attach a commit reference (and optional test summary) to a task."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        exists = db.session.execute(
            sa.select(CommitRef).where(
                CommitRef.task_id == task.id, CommitRef.sha == data["sha"]
            )
        ).scalar_one_or_none()
        if exists is None:
            db.session.add(CommitRef(task_id=task.id, **data))
        db.session.commit()
        return task


@blp.route("/<ident>/notes")
class TaskNotes(MethodView):
    @blp.response(200, NoteOut(many=True))
    def get(self, slug, ident):
        """List a task's notes, oldest first."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        return task.notes

    @blp.arguments(NoteIn)
    @blp.response(201, NoteOut)
    def post(self, data, slug, ident):
        """Add a timestamped note (comment) to a task."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        note = TaskNote(task_id=task.id, body=data["body"], author=data.get("author"))
        db.session.add(note)
        log_event(project.id, "note", agent=data.get("author"), task_id=task.id,
                  message=f"note on {task.display_id}: {data['body'][:120]}")
        db.session.commit()
        return note


@blp.route("/<ident>/relations")
class TaskRelations(MethodView):
    @blp.arguments(RelationIn)
    @blp.response(201, MessageOut)
    def post(self, data, slug, ident):
        """Add a blocks/supersedes/relates/follow_up edge to another task."""
        require_api_key()
        project = get_project_or_404(slug)
        src = get_task_or_404(project.id, ident)
        dst = get_task_or_404(project.id, data["target"])
        if src.id == dst.id:
            abort(422, message="A task cannot relate to itself.")
        kind = RelationKind(data["kind"])
        exists = db.session.execute(
            sa.select(TaskRelation).where(
                TaskRelation.src_task_id == src.id,
                TaskRelation.dst_task_id == dst.id,
                TaskRelation.kind == kind,
            )
        ).scalar_one_or_none()
        if exists is None:
            db.session.add(TaskRelation(
                src_task_id=src.id, dst_task_id=dst.id, kind=kind
            ))
            if kind == RelationKind.supersedes:
                dst.status = TaskStatus.superseded
                dst.superseded_by_task_id = src.id
        db.session.commit()
        return {"message": f"{src.display_id} {kind.value} {dst.display_id}"}
