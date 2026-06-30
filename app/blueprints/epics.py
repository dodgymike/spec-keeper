"""Epic CRUD, scoped to a project."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..extensions import db
from ..helpers import get_epic_or_404, get_project_or_404, require_api_key
from ..models import Epic
from ..schemas import EpicIn, EpicOut, EpicPatch

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
        project = get_project_or_404(slug)
        return db.session.execute(
            sa.select(Epic)
            .where(Epic.project_id == project.id)
            .order_by(Epic.position, Epic.key)
        ).scalars().all()

    @blp.arguments(EpicIn)
    @blp.response(201, EpicOut)
    def post(self, data, slug):
        """Create an epic."""
        require_api_key()
        project = get_project_or_404(slug)
        existing = db.session.execute(
            sa.select(Epic).where(
                Epic.project_id == project.id, Epic.key == data["key"]
            )
        ).scalar_one_or_none()
        if existing is not None:
            abort(409, message=f"Epic '{data['key']}' already exists.")
        epic = Epic(project_id=project.id, **data)
        db.session.add(epic)
        db.session.commit()
        return epic


@blp.route("/<key>")
class EpicItem(MethodView):
    @blp.response(200, EpicOut)
    def get(self, slug, key):
        """Get an epic."""
        require_api_key()
        project = get_project_or_404(slug)
        return get_epic_or_404(project.id, key)

    @blp.arguments(EpicPatch)
    @blp.response(200, EpicOut)
    def patch(self, data, slug, key):
        """Update an epic (move section, reorder, retitle)."""
        require_api_key()
        project = get_project_or_404(slug)
        epic = get_epic_or_404(project.id, key)
        for k, v in data.items():
            setattr(epic, k, v)
        db.session.commit()
        return epic
