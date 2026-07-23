"""SPEC.md round-trip: import a file into the DB and export the DB back to a file.

The migration bridge — a repo can run file-and-server in parallel, then go
server-only. ``/import`` and ``/export`` exchange raw ``text/markdown`` so an
agent can ``curl --data-binary @SPEC.md`` and ``curl ... > SPEC.md`` directly.
"""
from __future__ import annotations

from flask import Response, current_app, request
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_project_perm
from ..schemas import ImportResultOut, MessageOut
from ..specmd import normalize, parse_spec

blp = Blueprint(
    "ports", __name__, url_prefix="/api/v1/projects/<slug>",
    description="SPEC.md import/export round-trip (the migration bridge).",
)


@blp.route("/import")
class ImportSpec(MethodView):
    @blp.response(200, ImportResultOut)
    @blp.alt_response(207, schema=ImportResultOut, description=(
        "Multi-Status: some tasks failed validation and were skipped (see "
        "``failed``); the rest were imported."))
    @blp.alt_response(413, schema=MessageOut, description=(
        "Payload too large (over ``MAX_CONTENT_LENGTH``). The limit and its "
        "approximate task capacity are stated in the message."))
    def post(self, slug):
        """Import a SPEC.md (raw ``text/markdown`` body) into the project.

        Idempotent: re-importing the same file makes no duplicate tasks and, when
        nothing changed, no writes at all (``unchanged``). Batched so a full
        ~1,500-task backlog imports in a few seconds on both backends. Returns a
        structured ``{total, created, updated, unchanged, failed, ...}`` result;
        a malformed individual task is reported in ``failed`` (HTTP 207), not a
        500. An oversize body is rejected with 413, not a 500."""
        require_project_perm(slug, "write")
        parsed = parse_spec(request.get_data(as_text=True))
        counts = current_app.storage.import_spec(slug, parsed)
        failed = counts.get("failed") or []
        created, updated = counts["tasks_created"], counts["tasks_updated"]
        unchanged = counts.get("tasks_unchanged", 0)
        result = {
            "message": (
                f"imported: {created} task(s) created, {updated} updated, "
                f"{unchanged} unchanged"
                + (f", {len(failed)} failed" if failed else "")
                + f"; {counts['epics_created']} epic(s) created, "
                f"{counts['epics_updated']} updated."
            ),
            "total": created + updated + unchanged + len(failed),
            "created": created, "updated": updated, "unchanged": unchanged,
            "failed": failed,
            "epics_created": counts["epics_created"],
            "epics_updated": counts["epics_updated"],
        }
        return (result, 207) if failed else result


@blp.route("/export")
class ExportSpec(MethodView):
    def get(self, slug):
        """Render the project's backlog back to a SPEC.md (``text/markdown``)."""
        require_project_perm(slug, "read")
        return Response(current_app.storage.render_spec_text(slug),
                        mimetype="text/markdown")


@blp.route("/export/diff")
class ExportDiff(MethodView):
    @blp.response(200, MessageOut)
    def post(self, slug):
        """Dry-run: compare a posted SPEC.md against what export would produce.
        Reports tasks that differ, so adoption is safe."""
        require_project_perm(slug, "write")
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
