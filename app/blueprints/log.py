"""Append-only event log (replaces AGENT_LOG.md) and decision records
(replaces DECISIONS.md)."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_project_perm
from ..schemas import (
    DecisionIn,
    DecisionOut,
    EventIn,
    EventOut,
    EventQuery,
    NoteQuery,
    ProjectNoteOut,
)

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
        require_project_perm(slug, "read")
        return current_app.storage.list_events(slug, args)

    @blp.arguments(EventIn)
    @blp.response(201, EventOut)
    def post(self, data, slug):
        """Append an event (e.g. a free-form note or a chain-step record)."""
        require_project_perm(slug, "write")
        return current_app.storage.create_event(slug, data)


@blp.route("/notes")
class ProjectNotes(MethodView):
    @blp.arguments(NoteQuery, location="query")
    @blp.response(200, ProjectNoteOut(many=True))
    def get(self, args, slug):
        """List notes across a project (newest first), tagged by scope.

        ``scope`` selects ``task``, ``epic``, or ``all`` (default). Filter with
        ``author``, ``task`` (key/public_id), ``epic`` (key), ``since`` (ISO
        time); paginate with ``limit``/``offset``."""
        require_project_perm(slug, "read")
        return current_app.storage.list_project_notes(slug, args)


@blp.route("/decisions")
class DecisionsCollection(MethodView):
    @blp.response(200, DecisionOut(many=True))
    def get(self, slug):
        """List decision records (newest first)."""
        require_project_perm(slug, "read")
        return current_app.storage.list_decisions(slug)

    @blp.arguments(DecisionIn)
    @blp.response(201, DecisionOut)
    def post(self, data, slug):
        """Record an ADR-style decision."""
        require_project_perm(slug, "write")
        return current_app.storage.create_decision(slug, data)
