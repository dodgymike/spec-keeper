"""Jira sync service (JIRA-8, refactored onto the storage port in SLS-J4).

Best-effort sync between the Spec Server task lifecycle and Jira Cloud. Both
public functions take ``(slug, task_dto)`` and go through ``current_app.storage``
and the backend-neutral DTOs, so sync works identically on BOTH backends. Both
are designed to NEVER raise — any failure is recorded on ``jira_sync_error`` and
emitted as an event (via ``storage.record_jira_sync``), but the calling code path
(task creation, task completion) always proceeds unimpeded.

Crypto boundary: the plaintext token is decrypted at THIS call site only and
handed to ``JiraClient``. It is never passed to the storage layer, never logged,
and never persisted in the clear.
"""
from __future__ import annotations

import logging

from flask import current_app

from .crypto import decrypt
from .jira_client import JiraClient
from .jira_transitions import find_transition
from .storage.dto import TaskDTO

logger = logging.getLogger(__name__)


def _build_client(config) -> JiraClient:
    """Construct a JiraClient from a JiraConfigDTO (token decrypted here only)."""
    token = decrypt(config.api_token_encrypted)
    return JiraClient(
        base_url=config.base_url,
        email=config.email,
        api_token=token,
    )


def sync_task_created(slug: str, task_dto: TaskDTO) -> None:
    """Create a Jira issue for the given task (best-effort, never raises).

    No-op conditions (returns immediately without error):
    - The project has no Jira config.
    - The Jira config exists but ``enabled`` is False.
    - The task already has a ``jira_issue_key`` (idempotent guard).
    """
    try:
        # Idempotent guard: already synced
        if task_dto.jira_issue_key:
            return

        config = current_app.storage.get_jira_config(slug)
        if config is None or not config.enabled:
            return

        client = _build_client(config)
        issue_key = client.create_issue(
            project_key=config.jira_project_key,
            summary=task_dto.title,
            description=task_dto.description or "",
            issue_type="Task",
        )

        current_app.storage.record_jira_sync(
            slug, task_dto.public_id, issue_key=issue_key
        )

    except Exception as exc:
        logger.warning(
            "Jira sync_task_created failed for task %s: %s",
            task_dto.public_id,
            exc,
        )
        try:
            current_app.storage.record_jira_sync(
                slug, task_dto.public_id, error=f"sync_task_created failed: {exc}"
            )
        except Exception:
            # Last-resort guard: even error recording must not raise
            logger.exception(
                "Failed to record jira_sync_error for task %s", task_dto.public_id
            )


def sync_task_completed(slug: str, task_dto: TaskDTO) -> None:
    """Transition the task's Jira issue to 'Done' (best-effort, never raises).

    No-op conditions (returns immediately without error):
    - The project has no Jira config.
    - The Jira config exists but ``enabled`` is False.

    If the task has no ``jira_issue_key`` yet, ``sync_task_created`` is called
    inline first (best-effort; the transition step proceeds regardless of
    whether the inline create succeeds).
    """
    try:
        config = current_app.storage.get_jira_config(slug)
        if config is None or not config.enabled:
            return

        issue_key = task_dto.jira_issue_key
        # Ensure the task has a Jira issue (may have been created before Jira
        # was enabled, or the earlier sync failed).
        if not issue_key:
            sync_task_created(slug, task_dto)
            # Re-read to pick up the freshly created key (if the inline create
            # succeeded); the DTO we were handed is a stale snapshot.
            issue_key = current_app.storage.get_task(
                slug, task_dto.public_id
            ).jira_issue_key

        # Proceed to transition regardless of whether create succeeded
        if not issue_key:
            # sync_task_created failed or was a no-op for another reason;
            # nothing to transition.
            return

        # Find the "Done" transition via the cached-transition lookup
        transition = find_transition(config, slug, "Done")
        transition_id = transition["id"]

        client = _build_client(config)
        client.transition_issue(issue_key, transition_id)

        # Clear any prior sync error on success (no version bump — D2).
        current_app.storage.record_jira_sync(slug, task_dto.public_id)

    except Exception as exc:
        logger.warning(
            "Jira sync_task_completed failed for task %s: %s",
            task_dto.public_id,
            exc,
        )
        try:
            current_app.storage.record_jira_sync(
                slug, task_dto.public_id, error=f"sync_task_completed failed: {exc}"
            )
        except Exception:
            logger.exception(
                "Failed to record jira_sync_error for task %s", task_dto.public_id
            )
