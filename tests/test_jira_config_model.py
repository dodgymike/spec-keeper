"""Tests for JiraProjectConfig model — verifies the table exists and round-trips."""
from __future__ import annotations

from app.extensions import db
from app.models import JiraProjectConfig, Project


def test_jira_config_model_create_and_read(app):
    """Round-trip: insert a JiraProjectConfig row and read it back."""
    with app.app_context():
        # Create prerequisite project
        project = Project(slug="jira-test", name="Jira Test")
        db.session.add(project)
        db.session.flush()

        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://example.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=None,
            jira_project_key="TEST",
            enabled=True,
            cached_transitions={"10": "To Do", "20": "In Progress", "30": "Done"},
        )
        db.session.add(config)
        db.session.commit()

        # Read back
        row = db.session.query(JiraProjectConfig).filter_by(
            project_id=project.id
        ).one()
        assert row.base_url == "https://example.atlassian.net"
        assert row.email == "agent@example.com"
        assert row.api_token_encrypted is None
        assert row.jira_project_key == "TEST"
        assert row.enabled is True
        assert row.cached_transitions == {"10": "To Do", "20": "In Progress", "30": "Done"}
        assert row.updated_at is not None


def test_jira_config_model_unique_project(app):
    """Only one config row per project (UNIQUE on project_id)."""
    import sqlalchemy as sa

    with app.app_context():
        project = Project(slug="jira-uniq", name="Jira Uniq")
        db.session.add(project)
        db.session.flush()

        c1 = JiraProjectConfig(
            project_id=project.id,
            base_url="https://a.atlassian.net",
            email="a@x.com",
            jira_project_key="A",
            enabled=False,
        )
        db.session.add(c1)
        db.session.commit()

        c2 = JiraProjectConfig(
            project_id=project.id,
            base_url="https://b.atlassian.net",
            email="b@x.com",
            jira_project_key="B",
            enabled=False,
        )
        db.session.add(c2)
        try:
            db.session.commit()
            assert False, "Should have raised IntegrityError"
        except sa.exc.IntegrityError:
            db.session.rollback()


def test_jira_config_model_cascade_delete(app):
    """Deleting the project cascades to jira_project_config."""
    with app.app_context():
        project = Project(slug="jira-cascade", name="Cascade")
        db.session.add(project)
        db.session.flush()

        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://c.atlassian.net",
            email="c@x.com",
            jira_project_key="C",
            enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        config_id = config.id
        db.session.delete(project)
        db.session.commit()

        assert db.session.get(JiraProjectConfig, config_id) is None
