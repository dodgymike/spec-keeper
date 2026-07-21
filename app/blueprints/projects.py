"""Project CRUD."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
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
        return current_app.storage.list_projects()

    @blp.arguments(ProjectIn)
    @blp.response(201, ProjectOut)
    def post(self, data):
        """Create a project."""
        require_api_key()
        return current_app.storage.create_project(data)


@blp.route("/<slug>")
class ProjectItem(MethodView):
    @blp.response(200, ProjectOut)
    def get(self, slug):
        """Get a project by slug."""
        require_api_key()
        return current_app.storage.get_project(slug)

    @blp.arguments(ProjectPatch)
    @blp.response(200, ProjectOut)
    def patch(self, data, slug):
        """Update a project."""
        require_api_key()
        return current_app.storage.update_project(slug, data)

    @blp.response(204)
    def delete(self, slug):
        """Delete a project (cascades to its tasks)."""
        require_api_key()
        current_app.storage.delete_project(slug)
        return ""
