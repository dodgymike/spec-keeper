"""SPEC.md round-trip: import a file into the DB and export the DB back to a file.

The migration bridge — a repo can run file-and-server in parallel, then go
server-only. ``/import`` and ``/export`` exchange raw ``text/markdown`` so an
agent can ``curl --data-binary @SPEC.md`` and ``curl ... > SPEC.md`` directly.
"""
from __future__ import annotations

import sqlalchemy as sa
from flask import Response, request
from flask.views import MethodView
from flask_smorest import Blueprint

from ..extensions import db
from ..helpers import get_project_or_404, require_api_key
from ..models import Epic, Task
from ..schemas import MessageOut
from ..services import import_spec
from ..specmd import normalize, parse_spec, render_spec

blp = Blueprint(
    "ports", __name__, url_prefix="/api/v1/projects/<slug>",
    description="SPEC.md import/export round-trip (the migration bridge).",
)


class _RenderTask:
    """Lightweight view object the renderer understands."""

    def __init__(self, t: Task, epic_key: str | None):
        self.key = t.key
        self.title = t.title
        self.description = t.description
        self.status = t.status.value
        self.priority = t.priority.value if t.priority else None
        self.component = t.component
        self.proof_cmd = t.proof_cmd
        self.section = t.section
        self.position = t.position
        self.epic_key = epic_key
        self.tag_keys = [tag.key for tag in t.tags]


def _render_project(project) -> str:
    epics = db.session.execute(
        sa.select(Epic).where(Epic.project_id == project.id)
    ).scalars().all()
    epic_key_by_id = {e.id: e.key for e in epics}
    tasks = db.session.execute(
        sa.select(Task).where(Task.project_id == project.id)
    ).scalars().all()
    render_tasks = [_RenderTask(t, epic_key_by_id.get(t.epic_id)) for t in tasks]
    return render_spec(project.name or project.slug, epics, render_tasks)


@blp.route("/import")
class ImportSpec(MethodView):
    @blp.response(200, MessageOut)
    def post(self, slug):
        """Import a SPEC.md (raw ``text/markdown`` body) into the project.
        Idempotent: re-importing the same file makes no duplicate tasks."""
        require_api_key()
        project = get_project_or_404(slug)
        text = request.get_data(as_text=True)
        parsed = parse_spec(text)
        counts = import_spec(project.id, parsed)
        db.session.commit()
        return {"message": (
            f"imported: {counts['tasks_created']} task(s) created, "
            f"{counts['tasks_updated']} updated; "
            f"{counts['epics_created']} epic(s) created, "
            f"{counts['epics_updated']} updated."
        )}


@blp.route("/export")
class ExportSpec(MethodView):
    def get(self, slug):
        """Render the project's backlog back to a SPEC.md (``text/markdown``)."""
        require_api_key()
        project = get_project_or_404(slug)
        return Response(_render_project(project), mimetype="text/markdown")


@blp.route("/export/diff")
class ExportDiff(MethodView):
    @blp.response(200, MessageOut)
    def post(self, slug):
        """Dry-run: compare a posted SPEC.md against what export would produce.
        Reports tasks that differ, so adoption is safe."""
        require_api_key()
        project = get_project_or_404(slug)
        posted = normalize(parse_spec(request.get_data(as_text=True)))
        current = normalize(parse_spec(_render_project(project)))

        posted_by = {r["key"]: r for r in posted}
        current_by = {r["key"]: r for r in current}
        added = sorted(set(posted_by) - set(current_by))
        removed = sorted(set(current_by) - set(posted_by))
        changed = sorted(
            k for k in set(posted_by) & set(current_by)
            if posted_by[k] != current_by[k]
        )
        return {"message": (
            f"diff vs posted: {len(added)} new ({added}), "
            f"{len(removed)} only-in-server ({removed}), "
            f"{len(changed)} changed ({changed})."
        )}
