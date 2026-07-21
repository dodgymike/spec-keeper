"""Tasks: CRUD plus the concurrency-critical claim/complete/reserve flows.

Blueprints are now thin: they validate/deserialize (Marshmallow), enforce auth
and parse HTTP-level concerns (If-Match, Idempotency-Key), then delegate every
data operation to ``current_app.storage`` (the storage abstraction, SLS-2).
The atomic guarantees live in the adapter, unchanged.
"""
from __future__ import annotations

from flask import Response, current_app, jsonify
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..helpers import (
    etag_headers,
    expected_version_from_request,
    require_api_key,
)
from ..idempotency import idempotency_key_from_request
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

blp = Blueprint(
    "tasks", __name__, url_prefix="/api/v1/projects/<slug>/tasks",
    description="The task backlog. Atomically claim, complete and assign tasks.",
)


@blp.route("")
class TasksCollection(MethodView):
    @blp.arguments(TaskQuery, location="query")
    @blp.response(200, TaskOut(many=True))
    def get(self, args, slug):
        """List/filter tasks. Filter by ``owner`` to see one agent's specs."""
        require_api_key()
        return current_app.storage.list_tasks(slug, args)

    @blp.arguments(TaskIn)
    @blp.response(201, TaskOut)
    def post(self, data, slug):
        """Create a task."""
        require_api_key()
        return current_app.storage.create_task(slug, data)


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
        result = current_app.storage.claim_next(
            slug,
            agent=data["agent"],
            epic=data.get("epic"),
            priority_max=data.get("priority_max"),
            component=data.get("component"),
            lease_ttl=data.get("lease_ttl"),
            idempotency_key=idempotency_key_from_request(),
            serialize=lambda task: TaskOut().dump(task),
        )
        if result.replay_body is not None:
            return jsonify(result.replay_body), result.replay_status
        if result.result is None:
            return Response(status=204)
        task = result.result
        return task, 200, etag_headers(task)


@blp.route("/<ident>")
class TaskItem(MethodView):
    @blp.response(200, TaskOut)
    def get(self, slug, ident):
        """Get a task by key or public_id. Returns an ETag for optimistic locking."""
        require_api_key()
        task = current_app.storage.get_task(slug, ident)
        return task, 200, etag_headers(task)

    @blp.arguments(TaskPatch)
    @blp.response(200, TaskOut)
    def patch(self, data, slug, ident):
        """Update task fields. Honours ``If-Match`` (412 on version conflict)."""
        require_api_key()
        task = current_app.storage.update_task(
            slug, ident, data, expected_version_from_request()
        )
        return task, 200, etag_headers(task)

    @blp.response(204)
    def delete(self, slug, ident):
        """Delete a task."""
        require_api_key()
        current_app.storage.delete_task(slug, ident)
        return ""


@blp.route("/<ident>/complete")
class TaskComplete(MethodView):
    @blp.arguments(CompleteIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Complete a task: flip to done, close its lease, record commit/proof.

        This is the spec-keeper 'flip the checkbox to [x]' operation."""
        require_api_key()
        task = current_app.storage.complete_task(
            slug, ident, data, expected_version_from_request()
        )
        return task, 200, etag_headers(task)


@blp.route("/<ident>/release")
class TaskRelease(MethodView):
    @blp.arguments(ReleaseIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Release a claimed task without completing it (back to todo)."""
        require_api_key()
        task = current_app.storage.release_task(slug, ident, data["reset_to"])
        return task, 200, etag_headers(task)


@blp.route("/<ident>/status")
class TaskStatusUpdate(MethodView):
    @blp.arguments(StatusIn)
    @blp.response(200, TaskOut)
    def post(self, data, slug, ident):
        """Set an explicit status (blocked/deferred/superseded/...) with a note."""
        require_api_key()
        task = current_app.storage.set_status(
            slug, ident, data["status"], data.get("note"), "note" in data,
            expected_version_from_request(),
        )
        return task, 200, etag_headers(task)


@blp.route("/<ident>/commits")
class TaskCommits(MethodView):
    @blp.arguments(CommitIn)
    @blp.response(201, TaskOut)
    def post(self, data, slug, ident):
        """Attach a commit reference (and optional test summary) to a task."""
        require_api_key()
        return current_app.storage.add_commit(slug, ident, data)


@blp.route("/<ident>/notes")
class TaskNotes(MethodView):
    @blp.response(200, NoteOut(many=True))
    def get(self, slug, ident):
        """List a task's notes, oldest first."""
        require_api_key()
        return current_app.storage.list_task_notes(slug, ident)

    @blp.arguments(NoteIn)
    @blp.response(201, NoteOut)
    def post(self, data, slug, ident):
        """Add a timestamped note (comment) to a task."""
        require_api_key()
        return current_app.storage.append_task_note(slug, ident, data)


@blp.route("/<ident>/relations")
class TaskRelations(MethodView):
    @blp.arguments(RelationIn)
    @blp.response(201, MessageOut)
    def post(self, data, slug, ident):
        """Add a blocks/supersedes/relates/follow_up edge to another task."""
        require_api_key()
        # Resolve both ends (404 if absent) and reject self-relations (422)
        # before mutating — mirrors the original blueprint's ordering.
        src = current_app.storage.get_task(slug, ident)
        dst = current_app.storage.get_task(slug, data["target"])
        if src.public_id == dst.public_id:
            abort(422, message="A task cannot relate to itself.")
        message = current_app.storage.add_relation(slug, ident, data["target"], data["kind"])
        return {"message": message}
