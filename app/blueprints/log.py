"""Append-only event log (replaces AGENT_LOG.md) and decision records
(replaces DECISIONS.md)."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_project_perm
from ..schemas import (
    ChangesHeadOut,
    ChangesPageOut,
    ChangesQuery,
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


def _min_retained_seq(slug: str) -> int:
    """The watermark: the largest cursor still fully serviceable from deltas.

    A client whose ``since >= min_retained_seq`` can be served entirely from the
    retained change-log; a smaller ``since`` predates the window (entries it needs
    were pruned) and must full-resync. It equals ``lowest_retained_seq - 1`` (0 when
    the log is empty or nothing has been pruned, so ``since=0`` stays serviceable).
    Nothing is pruned today, so this is 0 — but the value is derived from the
    actual lowest retained ``seq`` so a future TTL raises it automatically."""
    first = current_app.storage.list_changes(slug, 0, 1)
    return (first[0].seq - 1) if first else 0


@blp.route("/changes")
class ChangesFeed(MethodView):
    @blp.arguments(ChangesQuery, location="query")
    @blp.response(200, ChangesPageOut)
    def get(self, args, slug):
        """Incremental change feed (UI-DELTA-5): entries with ``seq > since`` in
        ascending order. ``cursor`` is the max seq in this page (or the head when
        empty); re-poll with it while ``truncated``. ``full_resync_required`` when
        the cursor predates the retained window. ETag is the cursor."""
        require_project_perm(slug, "read")
        since = args["since"]
        limit = args["limit"]
        changes = current_app.storage.list_changes(slug, since, limit)
        if changes:
            cursor = changes[-1].seq
        else:
            cursor = current_app.storage.changes_head(slug)
        min_retained = _min_retained_seq(slug)
        page = {
            "cursor": cursor,
            "changes": changes,
            "truncated": len(changes) == limit,
            "full_resync_required": since < min_retained,
            "min_retained_seq": min_retained,
        }
        return page, 200, {"ETag": f'"{cursor}"'}


@blp.route("/changes/head")
class ChangesHead(MethodView):
    @blp.response(200, ChangesHeadOut)
    def get(self, slug):
        """Cheap cursor read (UI-DELTA-5): the current head seq + retained
        watermark, for the idle poll. ETag is the cursor."""
        require_project_perm(slug, "read")
        cursor = current_app.storage.changes_head(slug)
        min_retained = _min_retained_seq(slug)
        return ({"cursor": cursor, "min_retained_seq": min_retained},
                200, {"ETag": f'"{cursor}"'})


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
