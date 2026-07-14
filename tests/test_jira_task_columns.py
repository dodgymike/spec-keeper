"""Tests for jira_issue_key and jira_sync_error columns on the Task model (JIRA-7)."""
from __future__ import annotations

from app.extensions import db
from app.models import Project, Task, TaskStatus


def test_jira_task_columns_exist_and_nullable(app):
    """The two Jira columns exist, are nullable, and round-trip correctly."""
    with app.app_context():
        project = Project(slug="jira-cols", name="Jira Columns Test")
        db.session.add(project)
        db.session.flush()

        # Create a task without setting jira columns — they should default to None
        task = Task(
            project_id=project.id,
            title="Test task without jira fields",
            status=TaskStatus.todo,
        )
        db.session.add(task)
        db.session.commit()

        row = db.session.get(Task, task.id)
        assert row.jira_issue_key is None
        assert row.jira_sync_error is None


def test_jira_task_columns_round_trip(app):
    """Set jira_issue_key and jira_sync_error, then read them back."""
    with app.app_context():
        project = Project(slug="jira-rt", name="Jira RT")
        db.session.add(project)
        db.session.flush()

        task = Task(
            project_id=project.id,
            title="Linked to Jira",
            status=TaskStatus.in_progress,
            jira_issue_key="PROJ-42",
            jira_sync_error="timeout connecting to Jira",
        )
        db.session.add(task)
        db.session.commit()

        row = db.session.get(Task, task.id)
        assert row.jira_issue_key == "PROJ-42"
        assert row.jira_sync_error == "timeout connecting to Jira"


def test_jira_task_columns_update(app):
    """Update jira columns on an existing task."""
    with app.app_context():
        project = Project(slug="jira-up", name="Jira Update")
        db.session.add(project)
        db.session.flush()

        task = Task(
            project_id=project.id,
            title="Will be linked",
            status=TaskStatus.todo,
        )
        db.session.add(task)
        db.session.commit()

        # Update jira fields
        task.jira_issue_key = "TEAM-99"
        task.jira_sync_error = None
        db.session.commit()

        row = db.session.get(Task, task.id)
        assert row.jira_issue_key == "TEAM-99"
        assert row.jira_sync_error is None

        # Now set an error
        task.jira_sync_error = "403 Forbidden"
        db.session.commit()

        row = db.session.get(Task, task.id)
        assert row.jira_sync_error == "403 Forbidden"


def test_jira_task_columns_filterable(app):
    """Can filter tasks by jira_issue_key."""
    with app.app_context():
        project = Project(slug="jira-filter", name="Jira Filter")
        db.session.add(project)
        db.session.flush()

        t1 = Task(
            project_id=project.id,
            title="Linked",
            status=TaskStatus.todo,
            jira_issue_key="ABC-1",
        )
        t2 = Task(
            project_id=project.id,
            title="Not linked",
            status=TaskStatus.todo,
        )
        db.session.add_all([t1, t2])
        db.session.commit()

        # Filter by jira_issue_key
        results = (
            db.session.query(Task)
            .filter(Task.jira_issue_key == "ABC-1")
            .all()
        )
        assert len(results) == 1
        assert results[0].title == "Linked"

        # Filter for NULL jira_issue_key
        results_null = (
            db.session.query(Task)
            .filter(Task.jira_issue_key.is_(None))
            .all()
        )
        assert len(results_null) == 1
        assert results_null[0].title == "Not linked"
