"""SPEC.md round-trip: import a file into the DB and export the DB back to a file.

The migration bridge — a repo can run file-and-server in parallel, then go
server-only. ``/import`` and ``/export`` exchange raw ``text/markdown`` so an
agent can ``curl --data-binary @SPEC.md`` and ``curl ... > SPEC.md`` directly.
"""
from __future__ import annotations

from flask import Response, current_app, request
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
from ..schemas import MessageOut
from ..specmd import normalize, parse_spec

blp = Blueprint(
    "ports", __name__, url_prefix="/api/v1/projects/<slug>",
    description="SPEC.md import/export round-trip (the migration bridge).",
)


@blp.route("/import")
class ImportSpec(MethodView):
    @blp.response(200, MessageOut)
    def post(self, slug):
        """Import a SPEC.md (raw ``text/markdown`` body) into the project.
        Idempotent: re-importing the same file makes no duplicate tasks."""
        require_api_key()
        parsed = parse_spec(request.get_data(as_text=True))
        counts = current_app.storage.import_spec(slug, parsed)
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
        return Response(current_app.storage.render_spec_text(slug),
                        mimetype="text/markdown")


@blp.route("/export/diff")
class ExportDiff(MethodView):
    @blp.response(200, MessageOut)
    def post(self, slug):
        """Dry-run: compare a posted SPEC.md against what export would produce.
        Reports tasks that differ, so adoption is safe."""
        require_api_key()
        posted = normalize(parse_spec(request.get_data(as_text=True)))
        current = normalize(parse_spec(current_app.storage.render_spec_text(slug)))

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
