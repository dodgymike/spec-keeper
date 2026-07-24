"""Manual retry endpoint for failed/missing Jira syncs (JIRA-11).

POST /projects/{slug}/jira/sync re-attempts sync for tasks that have
jira_sync_error set OR are missing jira_issue_key in a project with an
enabled Jira config.  Returns counts of synced/failed.

Storage-port rewrite (SLS-J5): enumerates candidate tasks via
``current_app.storage.list_tasks`` + an in-memory filter (no ORM/``db.session``,
no new GSI) and re-runs sync through the ``(slug, task_dto)`` signature, so the
retry path behaves identically on BOTH backends.
"""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..helpers import require_project_perm
from ..jira_sync import sync_task_completed, sync_task_created
from ..schemas import JiraSyncRetryOut

blp = Blueprint(
    "jira_sync_retry", __name__, url_prefix="/api/v1/projects",
    description="Manual retry for failed/missing Jira syncs.",
)

# Page size for the candidate scan; matches TaskQuery's max ``limit``.
_PAGE = 1000


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
        require_project_perm(slug, "write")

        # Verify that the project has an enabled Jira config (get_jira_config
        # raises NotFound -> 404 when the project itself is absent).
        config = current_app.storage.get_jira_config(slug)
        if config is None or not config.enabled:
            abort(404, message="No enabled Jira config for this project.")

        # Enumerate every task through the storage port, paging to be safe.
        candidates = []
        offset = 0
        while True:
            page = current_app.storage.list_tasks(
                slug, {"limit": _PAGE, "offset": offset}
            )
            candidates.extend(page)
            if len(page) < _PAGE:
                break
            offset += _PAGE

        # Retry-eligible: a previous attempt failed (``jira_sync_error`` set) OR
        # the task was never synced (``jira_issue_key is None``). Same intent as
        # the old SQL ``OR`` predicate, applied in memory over backend-neutral DTOs.
        eligible = [
            t for t in candidates
            if t.jira_sync_error is not None or t.jira_issue_key is None
        ]

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
                sync_task_created(slug, task)
            elif task.status.value == "done":
                sync_task_completed(slug, task)
            else:
                # Has jira_issue_key, not done — the error is stale; clear it.
                # record_jira_sync with no error clears jira_sync_error to None
                # without touching jira_issue_key or bumping the version (D2).
                current_app.storage.record_jira_sync(slug, task.public_id)

            # Re-read to observe the outcome (the DTO above is a stale snapshot).
            fresh = current_app.storage.get_task(slug, task.public_id)
            if fresh.jira_sync_error is None:
                synced += 1
            else:
                failed += 1

        return {"synced": synced, "failed": failed}
