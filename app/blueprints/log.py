"""Append-only event log (replaces AGENT_LOG.md) and decision records
(replaces DECISIONS.md)."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint

from ..extensions import db
from ..helpers import get_project_or_404, get_task_or_404, require_api_key
from ..models import Decision, Epic, EpicNote, Event, Task, TaskNote
from ..schemas import (
    DecisionIn,
    DecisionOut,
    EventIn,
    EventOut,
    EventQuery,
    NoteQuery,
    ProjectNoteOut,
)
from ..services import log_event

blp = Blueprint(
    "log", __name__, url_prefix="/api/v1/projects/<slug>",
    description="Append-only event log and decision records.",
)


@blp.route("/events")
class EventsCollection(MethodView):
    @blp.arguments(EventQuery, location="query")
    @blp.response(200, EventOut(many=True))
    def get(self, args, slug):
        """Read the event stream (newest first). Filter by type/agent/task."""
        require_api_key()
        project = get_project_or_404(slug)
        query = sa.select(Event).where(Event.project_id == project.id)
        if "event_type" in args:
            query = query.where(Event.event_type == args["event_type"])
        if "agent" in args:
            query = query.where(Event.agent == args["agent"])
        if "task" in args:
            task = get_task_or_404(project.id, args["task"])
            query = query.where(Event.task_id == task.id)
        query = (
            query.order_by(Event.created_at.desc(), Event.id.desc())
            .offset(args["offset"])
            .limit(args["limit"])
        )
        return db.session.execute(query).scalars().all()

    @blp.arguments(EventIn)
    @blp.response(201, EventOut)
    def post(self, data, slug):
        """Append an event (e.g. a free-form note or a chain-step record)."""
        require_api_key()
        project = get_project_or_404(slug)
        task_id = None
        if data.get("task_key"):
            task_id = get_task_or_404(project.id, data["task_key"]).id
        event = log_event(
            project.id, data["event_type"], agent=data.get("agent"),
            task_id=task_id, message=data.get("message"),
            payload=data.get("payload") or {},
        )
        db.session.commit()
        return event


@blp.route("/notes")
class ProjectNotes(MethodView):
    @blp.arguments(NoteQuery, location="query")
    @blp.response(200, ProjectNoteOut(many=True))
    def get(self, args, slug):
        """List notes across a project (newest first), tagged by scope.

        ``scope`` selects ``task``, ``epic``, or ``all`` (default). Filter with
        ``author``, ``task`` (key/public_id), ``epic`` (key), ``since`` (ISO
        time); paginate with ``limit``/``offset``."""
        require_api_key()
        project = get_project_or_404(slug)
        scope = args["scope"]
        cap = args["offset"] + args["limit"]
        rows: list[dict] = []

        want_task = scope in ("task", "all") and "epic" not in args
        want_epic = scope in ("epic", "all") and "task" not in args

        if want_task:
            q = (
                sa.select(TaskNote, Task.key, Task.public_id)
                .join(Task, Task.id == TaskNote.task_id)
                .where(Task.project_id == project.id)
            )
            if "author" in args:
                q = q.where(TaskNote.author == args["author"])
            if "task" in args:
                t = get_task_or_404(project.id, args["task"])
                q = q.where(TaskNote.task_id == t.id)
            if "since" in args:
                q = q.where(TaskNote.created_at >= args["since"])
            q = q.order_by(TaskNote.created_at.desc(), TaskNote.id.desc()).limit(cap)
            for n, key, pub in db.session.execute(q):
                rows.append({"scope": "task", "task": key or pub, "epic": None,
                             "author": n.author, "body": n.body,
                             "created_at": n.created_at})

        if want_epic:
            q = (
                sa.select(EpicNote, Epic.key)
                .join(Epic, Epic.id == EpicNote.epic_id)
                .where(Epic.project_id == project.id)
            )
            if "author" in args:
                q = q.where(EpicNote.author == args["author"])
            if "epic" in args:
                q = q.where(Epic.key == args["epic"])
            if "since" in args:
                q = q.where(EpicNote.created_at >= args["since"])
            q = q.order_by(EpicNote.created_at.desc(), EpicNote.id.desc()).limit(cap)
            for n, key in db.session.execute(q):
                rows.append({"scope": "epic", "task": None, "epic": key,
                             "author": n.author, "body": n.body,
                             "created_at": n.created_at})

        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[args["offset"]: args["offset"] + args["limit"]]


@blp.route("/decisions")
class DecisionsCollection(MethodView):
    @blp.response(200, DecisionOut(many=True))
    def get(self, slug):
        """List decision records (newest first)."""
        require_api_key()
        project = get_project_or_404(slug)
        return db.session.execute(
            sa.select(Decision)
            .where(Decision.project_id == project.id)
            .order_by(Decision.created_at.desc(), Decision.id.desc())
        ).scalars().all()

    @blp.arguments(DecisionIn)
    @blp.response(201, DecisionOut)
    def post(self, data, slug):
        """Record an ADR-style decision."""
        require_api_key()
        project = get_project_or_404(slug)
        task_id = None
        if data.get("task_key"):
            task_id = get_task_or_404(project.id, data.pop("task_key")).id
        else:
            data.pop("task_key", None)
        decision = Decision(project_id=project.id, task_id=task_id, **data)
        db.session.add(decision)
        log_event(project.id, "decision", agent=data.get("agent"), task_id=task_id,
                  message=f"decision: {data['title']}")
        db.session.commit()
        return decision
