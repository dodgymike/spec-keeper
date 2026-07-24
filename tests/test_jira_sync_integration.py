"""Integration tests for end-to-end Jira sync (JIRA-10).

Tests exercise the full HTTP API lifecycle (create task, complete task, retry)
with Jira HTTP calls mocked at the JiraClient boundary. Runs against real
Postgres -- no sqlite or in-memory substitutes.

Coverage that is genuinely NEW beyond JIRA-8/9/11's own tests:
- Full lifecycle: create -> Jira issue created -> complete -> Jira transition fired
- Jira down on create, recovers on complete (error stored then cleared)
- True end-to-end retry: create with Jira down -> error -> retry endpoint -> fixed
- Disabled-config no-op across full lifecycle (create + complete, zero Jira calls)
- Idempotent re-create: calling task-create endpoint twice with same key, only one
  Jira create call (duplicate key returns 409, no second Jira call)

Test marker: jira_sync (matches the task's proof_cmd: pytest -k jira_sync -q).
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
def integ_project(client):
    """Create a project for integration tests."""
    resp = client.post(
        "/api/v1/projects", json={"slug": "integ-proj", "name": "Integration Proj"}
    )
    assert resp.status_code == 201, resp.get_json()
    return "integ-proj"


@pytest.fixture
def jira_enabled(app, integ_project):
    """Create an enabled JiraProjectConfig with cached transitions."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == integ_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("test-token-not-real"),
            jira_project_key="INT",
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
def jira_disabled(app, integ_project):
    """Create a disabled JiraProjectConfig."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == integ_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("test-token-not-real"),
            jira_project_key="INT",
            enabled=False,
        )
        db.session.add(config)
        db.session.commit()
        return config.id


TASKS_URL = "/api/v1/projects/integ-proj/tasks"


class TestFullLifecycleSync:
    """End-to-end: create a task -> Jira issue created -> complete -> transitioned."""

    def test_create_then_complete_full_lifecycle(
        self, app, client, jira_enabled
    ):
        """A task created via HTTP gets a Jira issue; completing it transitions that issue."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-1",
        ) as mock_create, patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            # Step 1: Create the task
            resp = client.post(
                TASKS_URL,
                json={"title": "Lifecycle task", "key": "LIFE-1"},
            )
            assert resp.status_code == 201

            # Verify Jira create was called
            mock_create.assert_called_once_with(
                project_key="INT",
                summary="Lifecycle task",
                description="",
                issue_type="Task",
            )
            mock_transition.assert_not_called()

            # Verify issue key stored in DB
            with app.app_context():
                task = db.session.execute(
                    db.select(Task).where(Task.key == "LIFE-1")
                ).scalar_one()
                assert task.jira_issue_key == "INT-1"
                assert task.jira_sync_error is None

            # Step 2: Complete the task
            resp = client.post(
                f"{TASKS_URL}/LIFE-1/complete",
                json={"commit_sha": "abc123", "test_summary": "all pass"},
            )
            assert resp.status_code == 200
            assert resp.get_json()["status"] == "done"

            # Verify Jira transition was called
            mock_transition.assert_called_once_with("INT-1", "5")

            # Verify DB state after complete
            with app.app_context():
                task = db.session.execute(
                    db.select(Task).where(Task.key == "LIFE-1")
                ).scalar_one()
                assert task.jira_issue_key == "INT-1"
                assert task.jira_sync_error is None
                assert task.status.value == "done"


class TestJiraDownOnCreateRecoversOnComplete:
    """Jira fails on task create but succeeds on task complete."""

    def test_create_fails_complete_succeeds(self, app, client, jira_enabled):
        """Create stores error; complete transitions successfully (inline create)."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            side_effect=JiraClientError(
                503, "Service Unavailable", "POST", "http://jira/issue"
            ),
        ):
            resp = client.post(
                TASKS_URL,
                json={"title": "Recovering task", "key": "REC-1"},
            )
            assert resp.status_code == 201  # API always succeeds

        # Verify error stored, no issue key
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "REC-1")
            ).scalar_one()
            assert task.jira_issue_key is None
            assert task.jira_sync_error is not None
            assert "sync_task_created failed" in task.jira_sync_error

        # Now Jira recovers: complete triggers inline create + transition
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-2",
        ) as mock_create, patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            resp = client.post(
                f"{TASKS_URL}/REC-1/complete",
                json={"commit_sha": "def456"},
            )
            assert resp.status_code == 200
            assert resp.get_json()["status"] == "done"

            # Complete should have triggered inline create + transition
            mock_create.assert_called_once()
            mock_transition.assert_called_once_with("INT-2", "5")

        # Verify error cleared in DB
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "REC-1")
            ).scalar_one()
            assert task.jira_issue_key == "INT-2"
            assert task.jira_sync_error is None

    def test_create_succeeds_complete_fails(self, app, client, jira_enabled):
        """Create sets issue key; complete fails to transition (error stored)."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-3",
        ):
            resp = client.post(
                TASKS_URL,
                json={"title": "Transition-fail task", "key": "TF-1"},
            )
            assert resp.status_code == 201

        # Verify create succeeded
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "TF-1")
            ).scalar_one()
            assert task.jira_issue_key == "INT-3"
            assert task.jira_sync_error is None

        # Complete with Jira down for transitions
        with patch(
            "app.jira_sync.JiraClient.transition_issue",
            side_effect=JiraClientError(
                500, "Internal Server Error", "POST", "http://jira/transition"
            ),
        ):
            resp = client.post(
                f"{TASKS_URL}/TF-1/complete",
                json={"commit_sha": "111aaa"},
            )
            # API still succeeds
            assert resp.status_code == 200
            assert resp.get_json()["status"] == "done"

        # Verify error recorded
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "TF-1")
            ).scalar_one()
            assert task.jira_issue_key == "INT-3"
            assert task.jira_sync_error is not None
            assert "sync_task_completed failed" in task.jira_sync_error


class TestRetryEndpointFixesPriorFailure:
    """True end-to-end: create with Jira down -> error -> retry -> fixed."""

    def test_retry_fixes_failed_create(self, app, client, jira_enabled):
        """Create task with Jira down, then retry endpoint with Jira up clears error."""
        # Step 1: Create with Jira down
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            side_effect=JiraClientError(
                503, "Service Unavailable", "POST", "http://jira/issue"
            ),
        ):
            resp = client.post(
                TASKS_URL,
                json={"title": "Retry me", "key": "RETRY-1"},
            )
            assert resp.status_code == 201

        # Verify error stored
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "RETRY-1")
            ).scalar_one()
            assert task.jira_issue_key is None
            assert task.jira_sync_error is not None

        # Step 2: Retry endpoint with Jira now up
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-10",
        ) as mock_create:
            resp = client.post("/api/v1/projects/integ-proj/jira/sync")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["synced"] >= 1
            assert data["failed"] == 0
            mock_create.assert_called()

        # Verify error cleared and issue key stored
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "RETRY-1")
            ).scalar_one()
            assert task.jira_issue_key == "INT-10"
            assert task.jira_sync_error is None

    def test_retry_fixes_failed_complete_transition(
        self, app, client, jira_enabled
    ):
        """Complete with transition failure, then retry with Jira up clears error."""
        # Step 1: Create successfully
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-11",
        ):
            resp = client.post(
                TASKS_URL,
                json={"title": "Retry transition", "key": "RETRY-2"},
            )
            assert resp.status_code == 201

        # Step 2: Complete with transition failure
        with patch(
            "app.jira_sync.JiraClient.transition_issue",
            side_effect=JiraClientError(
                500, "Server Error", "POST", "http://jira/transition"
            ),
        ):
            resp = client.post(
                f"{TASKS_URL}/RETRY-2/complete",
                json={"commit_sha": "bad1"},
            )
            assert resp.status_code == 200

        # Verify error stored
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "RETRY-2")
            ).scalar_one()
            assert task.jira_issue_key == "INT-11"
            assert task.jira_sync_error is not None
            assert "sync_task_completed failed" in task.jira_sync_error
            assert task.status.value == "done"

        # Step 3: Retry with Jira recovered
        with patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            resp = client.post("/api/v1/projects/integ-proj/jira/sync")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["synced"] == 1
            assert data["failed"] == 0
            mock_transition.assert_called_once_with("INT-11", "5")

        # Verify error cleared
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "RETRY-2")
            ).scalar_one()
            assert task.jira_sync_error is None


class TestDisabledConfigNoOpLifecycle:
    """Disabled config: full lifecycle with zero Jira calls."""

    def test_create_and_complete_with_disabled_config(
        self, app, client, jira_disabled
    ):
        """Neither create nor complete triggers any Jira call when config is disabled."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
        ) as mock_create, patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            # Create task
            resp = client.post(
                TASKS_URL,
                json={"title": "Disabled config task", "key": "DIS-1"},
            )
            assert resp.status_code == 201
            mock_create.assert_not_called()

            # Complete task
            resp = client.post(
                f"{TASKS_URL}/DIS-1/complete",
                json={"commit_sha": "dis123"},
            )
            assert resp.status_code == 200
            assert resp.get_json()["status"] == "done"
            mock_create.assert_not_called()
            mock_transition.assert_not_called()

        # Verify no issue key or error stored
        with app.app_context():
            task = db.session.execute(
                db.select(Task).where(Task.key == "DIS-1")
            ).scalar_one()
            assert task.jira_issue_key is None
            assert task.jira_sync_error is None

    def test_no_config_at_all_lifecycle(self, app, client, integ_project):
        """Neither create nor complete triggers any Jira call when no config exists."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
        ) as mock_create, patch(
            "app.jira_sync.JiraClient.transition_issue",
        ) as mock_transition:
            # Create task (no Jira config for project at all)
            resp = client.post(
                TASKS_URL,
                json={"title": "No config task", "key": "NC-1"},
            )
            assert resp.status_code == 201
            mock_create.assert_not_called()

            # Complete task
            resp = client.post(
                f"{TASKS_URL}/NC-1/complete",
                json={"commit_sha": "nc789"},
            )
            assert resp.status_code == 200
            mock_create.assert_not_called()
            mock_transition.assert_not_called()


class TestIdempotentReCreate:
    """Idempotent re-create: calling create twice for same key = only one Jira call."""

    def test_duplicate_key_returns_409_no_second_jira_call(
        self, app, client, jira_enabled
    ):
        """Second POST with same key returns 409; Jira create only called once."""
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-20",
        ) as mock_create:
            # First create succeeds
            resp = client.post(
                TASKS_URL,
                json={"title": "Unique task", "key": "IDEM-1"},
            )
            assert resp.status_code == 201
            assert mock_create.call_count == 1

            # Second create with same key: 409 before any Jira call
            resp = client.post(
                TASKS_URL,
                json={"title": "Unique task again", "key": "IDEM-1"},
            )
            assert resp.status_code == 409
            # Jira create was NOT called a second time
            assert mock_create.call_count == 1

    def test_sync_task_created_idempotent_when_key_already_set(
        self, app, client, jira_enabled
    ):
        """If a task already has jira_issue_key, the sync hook on a new task does not duplicate."""
        # Create via API -> Jira issue created
        with patch(
            "app.jira_sync.JiraClient.create_issue",
            return_value="INT-21",
        ) as mock_create:
            resp = client.post(
                TASKS_URL,
                json={"title": "Already synced", "key": "IDEM-2"},
            )
            assert resp.status_code == 201
            assert mock_create.call_count == 1

        # Manually invoke sync_task_created again (simulating a re-delivery). The
        # SLS-J4 signature is ``(slug, task_dto)``, so fetch the backend-neutral
        # DTO through the storage port rather than passing an ORM row.
        from app.jira_sync import sync_task_created

        with app.app_context():
            task_dto = app.storage.get_task("integ-proj", "IDEM-2")
            assert task_dto.jira_issue_key == "INT-21"

            with patch(
                "app.jira_sync.JiraClient.create_issue",
            ) as mock_create_2:
                sync_task_created("integ-proj", task_dto)
                # Must NOT call create again because jira_issue_key is already set
                mock_create_2.assert_not_called()

            # Unchanged after the idempotent no-op.
            assert app.storage.get_task(
                "integ-proj", "IDEM-2"
            ).jira_issue_key == "INT-21"
