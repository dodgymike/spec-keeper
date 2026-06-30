"""LOG-3: chain-run tracking. Records each execution of a task's mandated
agent chain (spec-keeper -> implementer -> reviewer -> security ...) and the
status of every step, enforcing that a skipped step carries a justification."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..extensions import db
from ..helpers import get_project_or_404, get_task_or_404, require_api_key
from ..models import ChainRun, ChainStep, utcnow
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


def _get_run_or_404(project_id: int, run_pubid: str) -> ChainRun:
    run = db.session.execute(
        sa.select(ChainRun).where(
            ChainRun.project_id == project_id, ChainRun.public_id == run_pubid
        )
    ).scalar_one_or_none()
    if run is None:
        abort(404, message=f"Chain run '{run_pubid}' not found.")
    return run


@blp.route("/tasks/<ident>/chain-runs")
class ChainRunsCollection(MethodView):
    @blp.arguments(ChainRunIn)
    @blp.response(201, ChainRunOut)
    def post(self, data, slug, ident):
        """Start a new chain run for a task."""
        require_api_key()
        project = get_project_or_404(slug)
        task = get_task_or_404(project.id, ident)
        run = ChainRun(
            project_id=project.id,
            task_id=task.id,
            started_by=data.get("started_by"),
            status="running",
        )
        db.session.add(run)
        db.session.commit()
        return run


@blp.route("/chain-runs/<run_pubid>")
class ChainRunItem(MethodView):
    @blp.response(200, ChainRunOut)
    def get(self, slug, run_pubid):
        """Get a chain run (and its steps) by public_id."""
        require_api_key()
        project = get_project_or_404(slug)
        return _get_run_or_404(project.id, run_pubid)

    @blp.arguments(ChainRunPatch)
    @blp.response(200, ChainRunOut)
    def patch(self, data, slug, run_pubid):
        """Update a run's status; terminal statuses stamp finished_at."""
        require_api_key()
        project = get_project_or_404(slug)
        run = _get_run_or_404(project.id, run_pubid)
        if "status" in data:
            run.status = data["status"]
            if data["status"] in ("passed", "failed", "aborted"):
                run.finished_at = utcnow()
        db.session.commit()
        return run


@blp.route("/chain-runs/<run_pubid>/steps/<step_name>")
class ChainRunStep(MethodView):
    @blp.arguments(ChainStepIn)
    @blp.response(200, ChainStepOut)
    def put(self, data, slug, run_pubid, step_name):
        """Create or update a step within a chain run (upsert by step_name)."""
        require_api_key()
        project = get_project_or_404(slug)
        run = _get_run_or_404(project.id, run_pubid)

        # The path is authoritative for the step name.
        data["step_name"] = step_name

        if data["status"] == "skipped" and not data.get("skip_justification"):
            abort(422, message="A skipped step requires skip_justification.")

        step = db.session.execute(
            sa.select(ChainStep).where(
                ChainStep.run_id == run.id, ChainStep.step_name == step_name
            )
        ).scalar_one_or_none()
        if step is None:
            step = ChainStep(run_id=run.id, step_name=step_name)
            db.session.add(step)

        step.step_order = data["step_order"]
        step.agent = data.get("agent")
        step.status = data["status"]
        step.skip_justification = data.get("skip_justification")
        step.output_ref = data.get("output_ref")

        db.session.commit()
        return step
