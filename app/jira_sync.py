"""Jira sync service (JIRA-8).

Best-effort sync between the Spec Server task lifecycle and Jira Cloud.
Both public functions are designed to NEVER raise — any failure is recorded on
`task.jira_sync_error` and emitted as an event, but the calling code path
(task creation, task completion) always proceeds unimpeded.
"""
from __future__ import annotations

import logging

from .crypto import decrypt
from .extensions import db
from .jira_client import JiraClient, JiraClientError
from .jira_transitions import (
    TransitionCacheError,
    TransitionNotFoundError,
    find_transition,
)
from .models import Event, JiraProjectConfig, Task
from .services import log_event

logger = logging.getLogger(__name__)


def _get_enabled_config(task: Task) -> JiraProjectConfig | None:
    """Return the project's enabled JiraProjectConfig, or None (no-op signal)."""
    config = db.session.execute(
        db.select(JiraProjectConfig).where(
            JiraProjectConfig.project_id == task.project_id,
            JiraProjectConfig.enabled.is_(True),
        )
    ).scalar_one_or_none()
    return config


def _record_error(task: Task, error_message: str) -> None:
    """Persist error to task and emit an event. Never raises."""
    task.jira_sync_error = error_message
    log_event(
        project_id=task.project_id,
        event_type="jira_sync_error",
        task_id=task.id,
        message=error_message,
    )
    db.session.commit()


def sync_task_created(task: Task) -> None:
    """Create a Jira issue for the given task (best-effort, never raises).

    No-op conditions (returns immediately without error):
    - The task's project has no JiraProjectConfig row.
    - The JiraProjectConfig exists but ``enabled`` is False.
    - The task already has a ``jira_issue_key`` (idempotent guard).
    """
    try:
        # Idempotent guard: already synced
        if task.jira_issue_key:
            return

        config = _get_enabled_config(task)
        if config is None:
            return

        # Decrypt token in-memory only
        token = decrypt(config.api_token_encrypted)
        client = JiraClient(
            base_url=config.base_url,
            email=config.email,
            api_token=token,
        )

        issue_key = client.create_issue(
            project_key=config.jira_project_key,
            summary=task.title,
            description=task.description or "",
            issue_type="Task",
        )

        task.jira_issue_key = issue_key
        task.jira_sync_error = None
        db.session.commit()

    except Exception as exc:
        logger.warning(
            "Jira sync_task_created failed for task %s: %s",
            task.id,
            exc,
        )
        try:
            _record_error(task, f"sync_task_created failed: {exc}")
        except Exception:
            # Last-resort guard: even error recording must not raise
            logger.exception("Failed to record jira_sync_error for task %s", task.id)


def sync_task_completed(task: Task) -> None:
    """Transition the task's Jira issue to 'Done' (best-effort, never raises).

    No-op conditions (returns immediately without error):
    - The task's project has no JiraProjectConfig row.
    - The JiraProjectConfig exists but ``enabled`` is False.

    If the task has no ``jira_issue_key`` yet, ``sync_task_created`` is called
    inline first (best-effort; the transition step proceeds regardless of
    whether the inline create succeeds).
    """
    try:
        config = _get_enabled_config(task)
        if config is None:
            return

        # Ensure the task has a Jira issue (may have been created before Jira
        # was enabled, or the earlier sync failed).
        if not task.jira_issue_key:
            sync_task_created(task)

        # Proceed to transition regardless of whether create succeeded
        if not task.jira_issue_key:
            # sync_task_created failed or was a no-op for another reason;
            # nothing to transition.
            return

        # Find the "Done" transition via the cached-transition lookup
        transition = find_transition(config, "Done")
        transition_id = transition["id"]

        # Build the client for the transition call
        token = decrypt(config.api_token_encrypted)
        client = JiraClient(
            base_url=config.base_url,
            email=config.email,
            api_token=token,
        )

        client.transition_issue(task.jira_issue_key, transition_id)

        # Clear any prior sync error on success
        task.jira_sync_error = None
        db.session.commit()

    except Exception as exc:
        logger.warning(
            "Jira sync_task_completed failed for task %s: %s",
            task.id,
            exc,
        )
        try:
            _record_error(task, f"sync_task_completed failed: {exc}")
        except Exception:
            logger.exception("Failed to record jira_sync_error for task %s", task.id)
