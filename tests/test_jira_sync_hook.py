"""Tests for JIRA-9: sync hooks wired into task create/complete endpoints.

Verifies that:
- Creating a task with a mocked-success Jira config sets jira_issue_key on the DB row.
- Creating a task with a mocked-failure Jira config still returns 201 (2xx) and
  sets jira_sync_error on the DB row.
- Completing a task triggers sync_task_completed.

NOTE: JIRA-12 (exposing jira_issue_key/jira_sync_error in TaskOut) has NOT landed,
so we verify via direct DB queries rather than the API response body.

Test marker: jira_sync_hook (matches the task's proof_cmd).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.extensions import db
from app.jira_client import JiraClientError
from app.models import JiraProjectConfig, Project, Task


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure the Fernet key env var is set for all tests in this module."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


@pytest.fixture
def hook_project(client):
    """Create a project for hook tests."""
    resp = client.post(
        "/api/v1/projects", json={"slug": "hook-proj", "name": "Hook Proj"}
    )
    assert resp.status_code == 201, resp.get_json()
    return "hook-proj"


@pytest.fixture
def jira_config_enabled(app, hook_project):
    """Create an enabled JiraProjectConfig for the hook-proj project."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == hook_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("secret-token"),
            jira_project_key="HOOK",
            enabled=True,
            cached_transitions={
                "statuses": [
                    {"id": "1", "name": "Open"},
                    {"id": "5", "name": "Done"},
                ],
                "fetched_at": "2026-07-01T00:00:00+00:00",
            },
        )
        db.session.add(config)
        db.session.commit()
        return config.id


BASE = "/api/v1/projects/hook-proj/tasks"


class TestCreateTaskSyncHook:
    """Task creation endpoint triggers sync_task_created after commit."""

    @pytest.mark.parametrize("marker", ["jira_sync_hook"])
    def test_create_task_success_sync_sets_jira_issue_key(
        self, app, client, jira_config_enabled, marker
    ):
        """Creating a task with successful Jira sync sets jira_issue_key on DB row."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="HOOK-100",
        ) as mock_create:
            resp = client.post(
                BASE,
                json={"title": "New feature", "key": "JSH-1"},
            )

        assert resp.status_code == 201
        mock_create.assert_called_once_with(
            project_key="HOOK",
            summary="New feature",
            description="",
            issue_type="Task",
        )

        # Verify via DB (TaskOut doesn't expose jira_issue_key yet — JIRA-12)
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "JSH-1")
            ).scalar_one()
            assert task.jira_issue_key == "HOOK-100"
            assert task.jira_sync_error is None

    @pytest.mark.parametrize("marker", ["jira_sync_hook"])
    def test_create_task_failed_sync_still_returns_201(
        self, app, client, jira_config_enabled, marker
    ):
        """Creating a task with failed Jira sync still returns 201; error is on DB row."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            side_effect=JiraClientError(503, "Service Unavailable", "POST", "http://x"),
        ):
            resp = client.post(
                BASE,
                json={"title": "Another feature", "key": "JSH-2"},
            )

        # The API must still return 201 regardless of sync failure
        assert resp.status_code == 201

        # Verify sync error recorded on DB row
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "JSH-2")
            ).scalar_one()
            assert task.jira_issue_key is None
            assert task.jira_sync_error is not None
            # SEC-FIX-1: bounded reader-safe message (status only, no upstream body).
            assert task.jira_sync_error == "sync failed (HTTP 503)"
            assert "Service Unavailable" not in task.jira_sync_error

    @pytest.mark.parametrize("marker", ["jira_sync_hook"])
    def test_create_task_no_config_no_sync_call(
        self, client, hook_project, marker
    ):
        """Creating a task without any Jira config still succeeds (no-op sync)."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
        ) as mock_create:
            resp = client.post(
                BASE,
                json={"title": "Plain task", "key": "JSH-3"},
            )

        assert resp.status_code == 201
        mock_create.assert_not_called()


class TestCompleteTaskSyncHook:
    """Task completion endpoint triggers sync_task_completed after commit."""

    @pytest.mark.parametrize("marker", ["jira_sync_hook"])
    def test_complete_task_triggers_sync_completed(
        self, app, client, jira_config_enabled, marker
    ):
        """Completing a task calls sync_task_completed after the commit."""
        # Create the task first (mock the create sync too)
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="HOOK-200",
        ):
            resp = client.post(
                BASE,
                json={"title": "Complete me", "key": "JSH-4"},
            )
        assert resp.status_code == 201

        # Now complete — mock the transition call
        with patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            resp = client.post(
                f"{BASE}/JSH-4/complete",
                json={"commit_sha": "deadbeef", "test_summary": "all pass"},
            )

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "done"
        mock_transition.assert_called_once_with("HOOK-200", "5")

    @pytest.mark.parametrize("marker", ["jira_sync_hook"])
    def test_complete_task_sync_failure_still_returns_200(
        self, app, client, jira_config_enabled, marker
    ):
        """Completing a task returns 200 even if Jira sync fails."""
        # Create the task
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="HOOK-201",
        ):
            resp = client.post(
                BASE,
                json={"title": "Fail sync on complete", "key": "JSH-5"},
            )
        assert resp.status_code == 201

        # Complete with a sync failure
        with patch(
            "app.jira_sync.JiraClient.transition_issue",
            side_effect=JiraClientError(500, "Internal Server Error", "POST", "http://x"),
        ):
            resp = client.post(
                f"{BASE}/JSH-5/complete",
                json={"commit_sha": "cafe1234"},
            )

        # API must still return 200
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "done"

        # Verify sync error recorded on DB row
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "JSH-5")
            ).scalar_one()
            assert task.jira_sync_error is not None
            # SEC-FIX-1: bounded reader-safe message (status only, no upstream body).
            assert task.jira_sync_error == "sync failed (HTTP 500)"
            assert "Internal Server Error" not in task.jira_sync_error
