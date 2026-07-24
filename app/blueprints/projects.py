"""Project CRUD."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import (
    caller_is_global_admin,
    current_identity,
    require_api_key,
    require_project_perm,
)
from ..schemas import ProjectHeadsOut, ProjectIn, ProjectOut, ProjectPatch

blp = Blueprint(
    "projects", __name__, url_prefix="/api/v1/projects",
    description="Projects (one per repo/codebase).",
)


def _visible_projects(storage):
    """The projects the CALLER may see — the single source of truth for isolation
    scoping shared by ``GET /projects`` and the ``GET /projects/heads`` fan-out.

    With per-project isolation ON (ISO-4), a non-admin caller sees only the projects
    they are a member of (via the verified token's ``sub``); a global spec-admin sees
    all; with it OFF, every project is visible (today's behaviour). ``None`` sub while
    enforced -> fail closed (empty). The global read gate is applied by the caller."""
    projects = storage.list_projects()
    if not current_app.config.get("PROJECT_ISOLATION_ENFORCED"):
        return projects
    if caller_is_global_admin():
        return projects
    sub = (current_identity() or {}).get("sub")
    if not sub:
        return []  # fail closed: no verified identity -> no visible projects
    allowed = {m.project_slug for m in storage.list_projects_for_principal(sub)}
    return [p for p in projects if p.slug in allowed]


@blp.route("")
class ProjectsCollection(MethodView):
    @blp.response(200, ProjectOut(many=True))
    def get(self):
        """List projects.

        With per-project isolation ON (ISO-4), a non-admin caller sees only the
        projects they are a member of (via the verified token's ``sub``); a global
        spec-admin sees all. With it OFF, every project is listed (today's
        behaviour). This is not project-scoped (no ``slug``), so it applies the
        global read gate directly, then filters."""
        require_api_key()
        return _visible_projects(current_app.storage)

    @blp.arguments(ProjectIn)
    @blp.response(201, ProjectOut)
    def post(self, data):
        """Create a project (creator-auto-admin, ISO-4).

        The VERIFIED creator (the token's ``sub``) is atomically recorded as an
        ``admin`` member of the new project — regardless of the isolation flag, so
        the backlog is ready before the flag is flipped. When there is no
        authenticated identity (local/auth-off) the membership insert is skipped.
        The creator identity comes ONLY from the verified token, never the body."""
        require_api_key()
        identity = current_identity() or {}
        return current_app.storage.create_project(
            data,
            creator_sub=identity.get("sub"),
            creator_name=identity.get("username"),
        )


@blp.route("/heads")
class ProjectsHeads(MethodView):
    @blp.response(200, ProjectHeadsOut)
    def get(self):
        """Batched change-log head map (UI-DELTA-10).

        One request returns ``{"heads": {slug: {cursor, min_retained_seq}}}`` for the
        caller's VISIBLE projects, so a dashboard showing many projects decides which
        advanced in a SINGLE call instead of polling ``/changes/head`` once per
        project (an N-request fan-out). Isolation is respected EXACTLY like
        ``GET /projects`` — the map is scoped to the same visible-project set, so a
        non-member's project head is never present (no cross-project leak). The input
        is bounded by that visible set (no caller-supplied slug list), so the fan-out
        cannot be widened. Reuses the per-project ``changes_head`` primitive on both
        backends via the ``changes_heads_for`` storage port."""
        require_api_key()
        storage = current_app.storage
        slugs = [p.slug for p in _visible_projects(storage)]
        return {"heads": storage.changes_heads_for(slugs)}


@blp.route("/<slug>")
class ProjectItem(MethodView):
    @blp.response(200, ProjectOut)
    def get(self, slug):
        """Get a project by slug."""
        require_project_perm(slug, "read")
        return current_app.storage.get_project(slug)

    @blp.arguments(ProjectPatch)
    @blp.response(200, ProjectOut)
    def patch(self, data, slug):
        """Update a project."""
        require_project_perm(slug, "admin")
        return current_app.storage.update_project(slug, data)

    @blp.response(204)
    def delete(self, slug):
        """Delete a project (cascades to its tasks)."""
        require_project_perm(slug, "admin")
        current_app.storage.delete_project(slug)
        return ""
