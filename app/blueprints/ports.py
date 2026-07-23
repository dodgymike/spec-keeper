"""SPEC.md round-trip: import a file into the DB and export the DB back to a file.

The migration bridge — a repo can run file-and-server in parallel, then go
server-only. ``/import`` and ``/export`` exchange raw ``text/markdown`` so an
agent can ``curl --data-binary @SPEC.md`` and ``curl ... > SPEC.md`` directly.
"""
from __future__ import annotations

from flask import Response, current_app, jsonify, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort
from marshmallow import ValidationError

from ..helpers import require_project_perm
from ..schemas import ExportDocOut, ImportResultOut, MessageOut
from ..specmd import normalize, parse_spec

blp = Blueprint(
    "ports", __name__, url_prefix="/api/v1/projects/<slug>",
    description="SPEC.md import/export round-trip (the migration bridge).",
)


def _import_result(counts: dict) -> dict:
    """Shape the storage adapter's counts into the structured ``ImportResultOut``
    body (shared by the SPEC.md and full-fidelity JSON import paths)."""
    failed = counts.get("failed") or []
    created, updated = counts["tasks_created"], counts["tasks_updated"]
    unchanged = counts.get("tasks_unchanged", 0)
    return {
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


def _import_json(slug: str) -> dict:
    """Parse + validate a full-fidelity JSON document (PORT-8) and upsert it,
    idempotent on each task's ``public_id``. A non-object body or a body that
    fails schema validation is a 400/422 (not a 500)."""
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        abort(400, message=(
            "Expected a JSON object: the full-fidelity export document "
            "({format, project, epics, tasks})."))
    try:
        doc = ExportDocOut().load(raw)
    except ValidationError as exc:
        abort(422, message=f"Invalid export document: {exc.messages}")
    return current_app.storage.import_doc(slug, doc)


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
        """Import a backlog into the project — idempotent, batched, size-capped.

        Two body formats are dispatched on ``Content-Type``:

        * ``text/markdown`` (default) — a SPEC.md file; tasks dedup on their human
          ``key`` (keyless tasks get a synthetic key). Behaviour unchanged.
        * ``application/json`` — the full-fidelity JSON document (PORT-8); tasks
          dedup on their stable ``public_id`` so KEYLESS tasks round-trip
          losslessly and a re-export -> re-import is a genuine no-op.

        Either way: re-importing an unchanged backlog makes no writes at all
        (``unchanged``); a malformed individual task is reported in ``failed``
        (HTTP 207), not a 500; an oversize body is rejected with 413, not a 500."""
        require_project_perm(slug, "write")
        ctype = (request.content_type or "").split(";", 1)[0].strip().lower()
        if ctype == "application/json":
            counts = _import_json(slug)
        else:
            parsed = parse_spec(request.get_data(as_text=True))
            counts = current_app.storage.import_spec(slug, parsed)
        result = _import_result(counts)
        return (result, 207) if result["failed"] else result


@blp.route("/export")
class ExportSpec(MethodView):
    def get(self, slug):
        """Export the project's backlog.

        Default (``text/markdown``): the human-readable SPEC.md mirror (keyed
        tasks only) — behaviour unchanged. Request the full-fidelity JSON
        migration document (PORT-8) — EVERY task, keyed AND keyless, on its stable
        ``public_id`` — with ``?format=json`` or ``Accept: application/json``."""
        require_project_perm(slug, "read")
        fmt = (request.args.get("format") or "").lower()
        want_json = fmt == "json" or (
            fmt == "" and request.accept_mimetypes.best == "application/json"
        )
        if want_json:
            doc = current_app.storage.export_doc(slug)
            return jsonify(ExportDocOut().dump(doc))
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
