"""Manual retry endpoint for failed/missing Jira syncs (JIRA-11).

POST /projects/{slug}/jira/sync re-attempts sync for tasks that have
jira_sync_error set OR are missing jira_issue_key in a project with an
enabled Jira config.  Returns counts of synced/failed.
"""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..extensions import db
from ..helpers import get_project_or_404, require_api_key
from ..jira_sync import sync_task_completed, sync_task_created
from ..models import JiraProjectConfig, Task
from ..schemas import JiraSyncRetryOut

blp = Blueprint(
    "jira_sync_retry", __name__, url_prefix="/api/v1/projects",
    description="Manual retry for failed/missing Jira syncs.",
)


@blp.route("/<slug>/jira/sync")
class JiraSyncRetry(MethodView):
    @blp.response(200, JiraSyncRetryOut)
    def post(self, slug):
        """Retry Jira sync for all tasks with errors or missing issue keys.

        Finds all tasks in the project that have ``jira_sync_error`` set OR
        are missing ``jira_issue_key`` (in a project with an enabled Jira
        config).  For each, calls the appropriate sync function.  Returns
        counts of successfully synced vs still-failed tasks.
        """
        require_api_key()
        project = get_project_or_404(slug)

        # Verify that the project has an enabled Jira config
        config = db.session.execute(
            sa.select(JiraProjectConfig).where(
                JiraProjectConfig.project_id == project.id,
                JiraProjectConfig.enabled.is_(True),
            )
        ).scalar_one_or_none()
        if config is None:
            abort(404, message="No enabled Jira config for this project.")

        # Find retry-eligible tasks:
        # 1. Tasks with jira_sync_error set (previous sync attempt failed)
        # 2. Tasks missing jira_issue_key (never synced / sync_task_created failed)
        eligible = db.session.execute(
            sa.select(Task).where(
                Task.project_id == project.id,
                sa.or_(
                    Task.jira_sync_error.isnot(None),
                    Task.jira_issue_key.is_(None),
                ),
            )
        ).scalars().all()

        synced = 0
        failed = 0

        for task in eligible:
            # Determine which sync step to retry:
            # - No jira_issue_key yet: needs create
            # - Has jira_issue_key + status done: needs complete transition
            # - Has jira_issue_key + status not done: the issue was created
            #   successfully but has a stale error; clear it (nothing to retry,
            #   the completion transition will run when the task is actually done)
            if not task.jira_issue_key:
                sync_task_created(task)
            elif task.status.value == "done":
                sync_task_completed(task)
            else:
                # Has jira_issue_key, not done — the error is stale; clear it.
                task.jira_sync_error = None
                db.session.commit()

            # Check outcome
            if task.jira_sync_error is None:
                synced += 1
            else:
                failed += 1

        return {"synced": synced, "failed": failed}
