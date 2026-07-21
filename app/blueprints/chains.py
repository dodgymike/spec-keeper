"""LOG-3: chain-run tracking. Records each execution of a task's mandated
agent chain (spec-keeper -> implementer -> reviewer -> security ...) and the
status of every step, enforcing that a skipped step carries a justification."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..helpers import require_api_key
from ..schemas import (
    ChainRunIn,
    ChainRunOut,
    ChainRunPatch,
    ChainStepIn,
    ChainStepOut,
)

blp = Blueprint(
    "chains", __name__, url_prefix="/api/v1/projects/<slug>",
    description="Chain-run and per-step tracking for a task's mandated agent chain.",
)


@blp.route("/tasks/<ident>/chain-runs")
class ChainRunsCollection(MethodView):
    @blp.arguments(ChainRunIn)
    @blp.response(201, ChainRunOut)
    def post(self, data, slug, ident):
        """Start a new chain run for a task."""
        require_api_key()
        return current_app.storage.create_chain_run(slug, ident, data.get("started_by"))


@blp.route("/chain-runs/<run_pubid>")
class ChainRunItem(MethodView):
    @blp.response(200, ChainRunOut)
    def get(self, slug, run_pubid):
        """Get a chain run (and its steps) by public_id."""
        require_api_key()
        return current_app.storage.get_chain_run(slug, run_pubid)

    @blp.arguments(ChainRunPatch)
    @blp.response(200, ChainRunOut)
    def patch(self, data, slug, run_pubid):
        """Update a run's status; terminal statuses stamp finished_at."""
        require_api_key()
        return current_app.storage.update_chain_run(slug, run_pubid, data.get("status"))


@blp.route("/chain-runs/<run_pubid>/steps/<step_name>")
class ChainRunStep(MethodView):
    @blp.arguments(ChainStepIn)
    @blp.response(200, ChainStepOut)
    def put(self, data, slug, run_pubid, step_name):
        """Create or update a step within a chain run (upsert by step_name)."""
        require_api_key()
        if data["status"] == "skipped" and not data.get("skip_justification"):
            abort(422, message="A skipped step requires skip_justification.")
        return current_app.storage.upsert_chain_step(slug, run_pubid, step_name, data)
