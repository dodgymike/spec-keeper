"""Epic CRUD, scoped to a project."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
from ..schemas import EpicIn, EpicOut, EpicPatch, NoteIn, NoteOut

blp = Blueprint(
    "epics", __name__, url_prefix="/api/v1/projects/<slug>/epics",
    description="Epics group tasks and own the task-ID prefix.",
)


@blp.route("")
class EpicsCollection(MethodView):
    @blp.response(200, EpicOut(many=True))
    def get(self, slug):
        """List a project's epics."""
        require_api_key()
        return current_app.storage.list_epics(slug)

    @blp.arguments(EpicIn)
    @blp.response(201, EpicOut)
    def post(self, data, slug):
        """Create an epic."""
        require_api_key()
        return current_app.storage.create_epic(slug, data)


@blp.route("/<key>")
class EpicItem(MethodView):
    @blp.response(200, EpicOut)
    def get(self, slug, key):
        """Get an epic."""
        require_api_key()
        return current_app.storage.get_epic(slug, key)

    @blp.arguments(EpicPatch)
    @blp.response(200, EpicOut)
    def patch(self, data, slug, key):
        """Update an epic (move section, reorder, retitle)."""
        require_api_key()
        return current_app.storage.update_epic(slug, key, data)


@blp.route("/<key>/notes")
class EpicNotes(MethodView):
    @blp.response(200, NoteOut(many=True))
    def get(self, slug, key):
        """List an epic's notes, oldest first."""
        require_api_key()
        return current_app.storage.list_epic_notes(slug, key)

    @blp.arguments(NoteIn)
    @blp.response(201, NoteOut)
    def post(self, data, slug, key):
        """Add a timestamped note to an epic (epic-level journal/reporting)."""
        require_api_key()
        return current_app.storage.append_epic_note(slug, key, data)
