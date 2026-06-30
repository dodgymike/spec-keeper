"""Project CRUD."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..extensions import db
from ..helpers import get_project_or_404, require_api_key
from ..models import Project
from ..schemas import ProjectIn, ProjectOut, ProjectPatch

blp = Blueprint(
    "projects", __name__, url_prefix="/api/v1/projects",
    description="Projects (one per repo/codebase).",
)


@blp.route("")
class ProjectsCollection(MethodView):
    @blp.response(200, ProjectOut(many=True))
    def get(self):
        """List projects."""
        require_api_key()
        return db.session.execute(
            sa.select(Project).order_by(Project.slug)
        ).scalars().all()

    @blp.arguments(ProjectIn)
    @blp.response(201, ProjectOut)
    def post(self, data):
        """Create a project."""
        require_api_key()
        existing = db.session.execute(
            sa.select(Project).where(Project.slug == data["slug"])
        ).scalar_one_or_none()
        if existing is not None:
            abort(409, message=f"Project '{data['slug']}' already exists.")
        project = Project(**data)
        db.session.add(project)
        db.session.commit()
        return project


@blp.route("/<slug>")
class ProjectItem(MethodView):
    @blp.response(200, ProjectOut)
    def get(self, slug):
        """Get a project by slug."""
        require_api_key()
        return get_project_or_404(slug)

    @blp.arguments(ProjectPatch)
    @blp.response(200, ProjectOut)
    def patch(self, data, slug):
        """Update a project."""
        require_api_key()
        project = get_project_or_404(slug)
        for k, v in data.items():
            setattr(project, k, v)
        db.session.commit()
        return project

    @blp.response(204)
    def delete(self, slug):
        """Delete a project (cascades to its tasks)."""
        require_api_key()
        project = get_project_or_404(slug)
        db.session.delete(project)
        db.session.commit()
        return ""
