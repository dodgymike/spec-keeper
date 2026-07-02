"""Tests for app/jira_sync.py — the Jira sync service (JIRA-8).

Covers:
- sync_task_created: happy path (creates issue, stores key)
- sync_task_created: Jira unreachable (sets jira_sync_error, does not raise)
- sync_task_created: disabled config (no-op)
- sync_task_created: missing config (no-op)
- sync_task_created: idempotent (already has issue_key, no duplicate create)
- sync_task_completed: happy path (transitions to Done)
- sync_task_completed: complete-without-prior-create (inline create then transition)
- sync_task_completed: transition failure (sets jira_sync_error, does not raise)
- sync_task_completed: disabled config (no-op)

All Jira HTTP calls are mocked; tests run against the real test Postgres DB.
Test marker: jira_sync_service (matches the task's proof_cmd).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.jira_client import JiraClientError
from app.jira_sync import sync_task_completed, sync_task_created
from app.models import Event, JiraProjectConfig, Project, Task, TaskStatus


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure the Fernet key env var is set for all tests in this module."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


@pytest.fixture
def sync_project(client):
    """Create a fresh project for sync tests."""
    resp = client.post(
        "/api/v1/projects", json={"slug": "sync-proj", "name": "Sync Proj"}
    )
    assert resp.status_code == 201, resp.get_json()
    return "sync-proj"


@pytest.fixture
def enabled_config(app, sync_project):
    """Create an enabled JiraProjectConfig for the sync-proj project."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == sync_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("secret-token"),
            jira_project_key="PROJ",
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


@pytest.fixture
def disabled_config(app, sync_project):
    """Create a disabled JiraProjectConfig for the sync-proj project."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == sync_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("secret-token"),
            jira_project_key="PROJ",
            enabled=False,
        )
        db.session.add(config)
        db.session.commit()
        return config.id


@pytest.fixture
def sample_task(app, sync_project):
    """Create a sample task in the sync-proj project."""
    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == sync_project)
        ).scalar_one()
        task = Task(
            project_id=project.id,
            title="Implement feature X",
            description="Detailed description of feature X",
            status=TaskStatus.todo,
        )
        db.session.add(task)
        db.session.commit()
        db.session.refresh(task)
        return task.id


class TestSyncTaskCreatedHappyPath:
    """sync_task_created: happy path creates issue and stores key."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_creates_issue(
        self, app, enabled_config, sample_task, marker
    ):
        """sync_task_created creates a Jira issue and stores the key."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"key": "PROJ-42"}

        with app.app_context():
            task = db.session.get(Task, sample_task)

            with patch(
                "app.jira_sync.JiraClient.create_issue",
                return_value="PROJ-42",
            ) as mock_create:
                sync_task_created(task)

            db.session.refresh(task)
            assert task.jira_issue_key == "PROJ-42"
            assert task.jira_sync_error is None
            mock_create.assert_called_once_with(
                project_key="PROJ",
                summary="Implement feature X",
                description="Detailed description of feature X",
                issue_type="Task",
            )


class TestSyncTaskCreatedFailure:
    """sync_task_created: failure sets jira_sync_error and emits event."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_error_on_jira_failure(
        self, app, enabled_config, sample_task, marker
    ):
        """When Jira is unreachable, task.jira_sync_error is set, no raise."""
        with app.app_context():
            task = db.session.get(Task, sample_task)

            with patch(
                "app.jira_sync.JiraClient.create_issue",
                side_effect=JiraClientError(503, "Service Unavailable", "POST", "http://x"),
            ):
                # Must NOT raise
                sync_task_created(task)

            db.session.refresh(task)
            assert task.jira_issue_key is None
            assert task.jira_sync_error is not None
            assert "sync_task_created failed" in task.jira_sync_error

            # Check event was emitted
            events = db.session.execute(
                db.select(Event).where(Event.event_type == "jira_sync_error")
            ).scalars().all()
            assert len(events) == 1
            assert events[0].task_id == task.id
            assert "secret-token" not in events[0].message


class TestSyncTaskCreatedNoOp:
    """sync_task_created: no-op cases."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_noop_disabled_config(
        self, app, disabled_config, sample_task, marker
    ):
        """sync_task_created is a no-op when config is disabled."""
        with app.app_context():
            task = db.session.get(Task, sample_task)

            with patch(
                "app.jira_sync.JiraClient.create_issue"
            ) as mock_create:
                sync_task_created(task)

            mock_create.assert_not_called()
            db.session.refresh(task)
            assert task.jira_issue_key is None
            assert task.jira_sync_error is None

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_noop_missing_config(
        self, app, sync_project, sample_task, marker
    ):
        """sync_task_created is a no-op when no config exists."""
        with app.app_context():
            task = db.session.get(Task, sample_task)

            with patch(
                "app.jira_sync.JiraClient.create_issue"
            ) as mock_create:
                sync_task_created(task)

            mock_create.assert_not_called()
            db.session.refresh(task)
            assert task.jira_issue_key is None
            assert task.jira_sync_error is None


class TestSyncTaskCreatedIdempotent:
    """sync_task_created: idempotent re-invocation does not create duplicate."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_idempotent_no_duplicate(
        self, app, enabled_config, sample_task, marker
    ):
        """Calling sync_task_created on a task that already has jira_issue_key skips."""
        with app.app_context():
            task = db.session.get(Task, sample_task)
            task.jira_issue_key = "PROJ-99"
            db.session.commit()

            with patch(
                "app.jira_sync.JiraClient.create_issue"
            ) as mock_create:
                sync_task_created(task)

            mock_create.assert_not_called()
            db.session.refresh(task)
            assert task.jira_issue_key == "PROJ-99"


class TestSyncTaskCompletedHappyPath:
    """sync_task_completed: happy path transitions issue to Done."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_transitions_to_done(
        self, app, enabled_config, sample_task, marker
    ):
        """sync_task_completed transitions the issue using the cached Done transition."""
        with app.app_context():
            task = db.session.get(Task, sample_task)
            task.jira_issue_key = "PROJ-42"
            db.session.commit()

            with patch(
                "app.jira_sync.JiraClient.transition_issue"
            ) as mock_transition:
                sync_task_completed(task)

            mock_transition.assert_called_once_with("PROJ-42", "5")
            db.session.refresh(task)
            assert task.jira_sync_error is None


class TestSyncTaskCompletedInlineCreate:
    """sync_task_completed: task without jira_issue_key triggers inline create."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_inline_create_then_transition(
        self, app, enabled_config, sample_task, marker
    ):
        """Complete on a task without issue_key first creates, then transitions."""
        with app.app_context():
            task = db.session.get(Task, sample_task)
            assert task.jira_issue_key is None

            with patch(
                "app.jira_sync.JiraClient.create_issue",
                return_value="PROJ-77",
            ) as mock_create, patch(
                "app.jira_sync.JiraClient.transition_issue"
            ) as mock_transition:
                sync_task_completed(task)

            mock_create.assert_called_once()
            mock_transition.assert_called_once_with("PROJ-77", "5")
            db.session.refresh(task)
            assert task.jira_issue_key == "PROJ-77"
            assert task.jira_sync_error is None


class TestSyncTaskCompletedFailure:
    """sync_task_completed: transition failure sets jira_sync_error."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_transition_error(
        self, app, enabled_config, sample_task, marker
    ):
        """Transition failure sets jira_sync_error but does not raise."""
        with app.app_context():
            task = db.session.get(Task, sample_task)
            task.jira_issue_key = "PROJ-42"
            db.session.commit()

            with patch(
                "app.jira_sync.JiraClient.transition_issue",
                side_effect=JiraClientError(500, "Server Error", "POST", "http://x"),
            ):
                # Must NOT raise
                sync_task_completed(task)

            db.session.refresh(task)
            assert task.jira_sync_error is not None
            assert "sync_task_completed failed" in task.jira_sync_error

            # Check event was emitted
            events = db.session.execute(
                db.select(Event).where(Event.event_type == "jira_sync_error")
            ).scalars().all()
            assert len(events) == 1
            assert "secret-token" not in events[0].message


class TestSyncTaskCompletedNoOp:
    """sync_task_completed: no-op when config disabled."""

    @pytest.mark.parametrize("marker", ["jira_sync_service"])
    def test_jira_sync_service_completed_noop_disabled(
        self, app, disabled_config, sample_task, marker
    ):
        """sync_task_completed is a no-op when config is disabled."""
        with app.app_context():
            task = db.session.get(Task, sample_task)

            with patch(
                "app.jira_sync.JiraClient.transition_issue"
            ) as mock_transition, patch(
                "app.jira_sync.JiraClient.create_issue"
            ) as mock_create:
                sync_task_completed(task)

            mock_transition.assert_not_called()
            mock_create.assert_not_called()
