"""Tests for the manual Jira sync retry endpoint (JIRA-11).

Covers:
- Retrying a task with jira_sync_error clears error on success (mocked Jira)
- Retrying a task that still fails leaves jira_sync_error set (counted as failed)
- Tasks without errors/missing keys are untouched if they don't meet retry criteria
- 404 for unknown project slug
- 404 when project has no enabled Jira config
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import JiraProjectConfig, Project, Task, TaskStatus


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure the Fernet key env var is set for all tests in this module."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


@pytest.fixture
def retry_project(client, app):
    """Create a project with an enabled Jira config for retry tests."""
    resp = client.post("/api/v1/projects", json={"slug": "retry-proj", "name": "Retry Project"})
    assert resp.status_code == 201, resp.get_json()

    # Create the Jira config
    from app.crypto import encrypt
    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == "retry-proj")
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="test@co.com",
            api_token_encrypted=encrypt("fake-token"),
            jira_project_key="TEST",
            enabled=True,
        )
        db.session.add(config)
        db.session.commit()

    return "retry-proj"


def _create_task(app, slug, title, **kwargs):
    """Helper to insert a task directly into the DB."""
    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == slug)
        ).scalar_one()
        task = Task(
            project_id=project.id,
            title=title,
            **kwargs,
        )
        db.session.add(task)
        db.session.commit()
        return task.id


class TestJiraSyncRetrySuccess:
    def test_retry_clears_error_on_success(self, client, app, retry_project):
        """A task with jira_sync_error gets retried and sync clears the error."""
        task_id = _create_task(
            app, retry_project, "Task with error",
            jira_sync_error="sync_task_created failed: timeout",
            jira_issue_key=None,
        )

        with patch("app.blueprints.jira_sync_retry.sync_task_created") as mock_sync:
            def clear_error(task):
                task.jira_issue_key = "TEST-1"
                task.jira_sync_error = None

            mock_sync.side_effect = clear_error

            resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
            assert resp.status_code == 200, resp.get_json()
            data = resp.get_json()
            assert data["synced"] == 1
            assert data["failed"] == 0

    def test_retry_completed_task_calls_sync_task_completed(self, client, app, retry_project):
        """A done task with jira_issue_key and jira_sync_error calls sync_task_completed."""
        task_id = _create_task(
            app, retry_project, "Done task with error",
            status=TaskStatus.done,
            jira_issue_key="TEST-99",
            jira_sync_error="sync_task_completed failed: connection reset",
        )

        with patch("app.blueprints.jira_sync_retry.sync_task_completed") as mock_sync:
            def clear_error(task):
                task.jira_sync_error = None

            mock_sync.side_effect = clear_error

            resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
            assert resp.status_code == 200, resp.get_json()
            data = resp.get_json()
            assert data["synced"] == 1
            assert data["failed"] == 0
            mock_sync.assert_called_once()


class TestJiraSyncRetryFailure:
    def test_retry_still_fails_counts_as_failed(self, client, app, retry_project):
        """A task where retry still fails is counted in 'failed'."""
        task_id = _create_task(
            app, retry_project, "Stubborn task",
            jira_sync_error="sync_task_created failed: 500",
            jira_issue_key=None,
        )

        with patch("app.blueprints.jira_sync_retry.sync_task_created") as mock_sync:
            # sync_task_created is best-effort and sets the error itself
            def keep_error(task):
                task.jira_sync_error = "sync_task_created failed: still broken"

            mock_sync.side_effect = keep_error

            resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
            assert resp.status_code == 200, resp.get_json()
            data = resp.get_json()
            assert data["synced"] == 0
            assert data["failed"] == 1

    def test_mixed_results(self, client, app, retry_project):
        """Multiple tasks: some succeed, some fail."""
        _create_task(
            app, retry_project, "Will succeed",
            jira_sync_error="sync_task_created failed: timeout",
            jira_issue_key=None,
        )
        _create_task(
            app, retry_project, "Will fail",
            jira_sync_error="sync_task_created failed: 403",
            jira_issue_key=None,
        )

        call_count = [0]

        with patch("app.blueprints.jira_sync_retry.sync_task_created") as mock_sync:
            def alternate(task):
                call_count[0] += 1
                if call_count[0] == 1:
                    task.jira_issue_key = "TEST-2"
                    task.jira_sync_error = None
                else:
                    task.jira_sync_error = "still broken"

            mock_sync.side_effect = alternate

            resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["synced"] == 1
            assert data["failed"] == 1


class TestJiraSyncRetryStaleError:
    def test_stale_error_cleared_for_non_done_task_with_key(self, client, app, retry_project):
        """A non-done task with jira_issue_key and jira_sync_error gets error cleared (stale)."""
        task_id = _create_task(
            app, retry_project, "In progress with stale error",
            status=TaskStatus.in_progress,
            jira_issue_key="TEST-50",
            jira_sync_error="sync_task_completed failed: old error",
        )

        # No mocking needed — the endpoint clears the error directly
        resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["synced"] == 1
        assert data["failed"] == 0

        # Verify the error was cleared in DB
        with app.app_context():
            task = db.session.get(Task, task_id)
            assert task.jira_sync_error is None
            assert task.jira_issue_key == "TEST-50"


class TestJiraSyncRetryNoEligible:
    def test_tasks_without_errors_not_retried(self, client, app, retry_project):
        """A task with jira_issue_key set and no error is not retry-eligible."""
        _create_task(
            app, retry_project, "Already synced",
            jira_issue_key="TEST-5",
            jira_sync_error=None,
        )

        resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["synced"] == 0
        assert data["failed"] == 0

    def test_missing_key_retried(self, client, app, retry_project):
        """A task with no jira_issue_key and no error IS eligible (never synced)."""
        _create_task(
            app, retry_project, "Never synced",
            jira_issue_key=None,
            jira_sync_error=None,
        )

        with patch("app.blueprints.jira_sync_retry.sync_task_created") as mock_sync:
            def create_issue(task):
                task.jira_issue_key = "TEST-10"
                task.jira_sync_error = None

            mock_sync.side_effect = create_issue

            resp = client.post(f"/api/v1/projects/{retry_project}/jira/sync")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["synced"] == 1
            assert data["failed"] == 0


class TestJiraSyncRetry404:
    def test_unknown_project_returns_404(self, client):
        """POST to a nonexistent project returns 404."""
        resp = client.post("/api/v1/projects/nonexistent/jira/sync")
        assert resp.status_code == 404

    def test_no_enabled_config_returns_404(self, client):
        """POST to a project without enabled Jira config returns 404."""
        # Create project without Jira config
        client.post("/api/v1/projects", json={"slug": "no-jira", "name": "No Jira"})
        resp = client.post("/api/v1/projects/no-jira/jira/sync")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "enabled Jira config" in data["message"]
