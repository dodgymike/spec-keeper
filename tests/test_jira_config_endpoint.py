"""Tests for the Jira config CRUD blueprint (JIRA-5).

Covers:
- POST creates a config (token encrypted at rest)
- GET never returns token/api_token_encrypted
- PUT updates an existing config
- 404 for unknown project
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import JiraProjectConfig, Project


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure the Fernet key env var is set for all tests in this module."""
    # This is a valid Fernet key for testing only.
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


@pytest.fixture
def jira_project(client):
    """Create a fresh project for jira config tests."""
    resp = client.post("/api/v1/projects", json={"slug": "jira-proj", "name": "Jira Project"})
    assert resp.status_code == 201, resp.get_json()
    return "jira-proj"


class TestJiraConfigPost:
    def test_create_config_encrypts_token(self, client, jira_project, app):
        """POST creates a config and the raw DB value differs from the plaintext token."""
        plaintext_token = "my-super-secret-jira-token-12345"
        resp = client.post(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://myco.atlassian.net",
            "email": "agent@myco.com",
            "api_token": plaintext_token,
            "jira_project_key": "PROJ",
            "enabled": True,
        })
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()

        # Response should have has_token=True but not the actual token
        assert data["has_token"] is True
        assert "api_token" not in data
        assert "api_token_encrypted" not in data
        assert data["base_url"] == "https://myco.atlassian.net"
        assert data["email"] == "agent@myco.com"
        assert data["jira_project_key"] == "PROJ"
        assert data["enabled"] is True

        # Verify raw DB value is NOT the plaintext
        with app.app_context():
            project = db.session.execute(
                db.select(Project).where(Project.slug == jira_project)
            ).scalar_one()
            config = db.session.execute(
                db.select(JiraProjectConfig).where(
                    JiraProjectConfig.project_id == project.id
                )
            ).scalar_one()
            assert config.api_token_encrypted is not None
            assert config.api_token_encrypted != plaintext_token
            # The encrypted value should be a Fernet token (starts with gAAAAA...)
            assert len(config.api_token_encrypted) > len(plaintext_token)

    def test_create_config_409_if_exists(self, client, jira_project):
        """POST returns 409 if config already exists."""
        payload = {
            "base_url": "https://a.atlassian.net",
            "email": "a@x.com",
            "api_token": "tok1",
            "jira_project_key": "A",
        }
        resp1 = client.post(f"/api/v1/projects/{jira_project}/jira-config", json=payload)
        assert resp1.status_code == 201

        resp2 = client.post(f"/api/v1/projects/{jira_project}/jira-config", json=payload)
        assert resp2.status_code == 409


class TestJiraConfigGet:
    def test_get_config_no_token_in_response(self, client, jira_project):
        """GET never returns api_token or api_token_encrypted."""
        client.post(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://myco.atlassian.net",
            "email": "agent@myco.com",
            "api_token": "secret-token",
            "jira_project_key": "PROJ",
            "enabled": True,
        })

        resp = client.get(f"/api/v1/projects/{jira_project}/jira-config")
        assert resp.status_code == 200
        data = resp.get_json()

        # Token must never appear
        assert "api_token" not in data
        assert "api_token_encrypted" not in data
        # But has_token indicates one is set
        assert data["has_token"] is True
        assert data["base_url"] == "https://myco.atlassian.net"

    def test_get_config_404_no_config(self, client, jira_project):
        """GET returns 404 if no config exists."""
        resp = client.get(f"/api/v1/projects/{jira_project}/jira-config")
        assert resp.status_code == 404


class TestJiraConfigPut:
    def test_update_config(self, client, jira_project):
        """PUT updates fields and re-encrypts token if provided."""
        # Create first
        client.post(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://old.atlassian.net",
            "email": "old@x.com",
            "api_token": "old-token",
            "jira_project_key": "OLD",
            "enabled": False,
        })

        # Update
        resp = client.put(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://new.atlassian.net",
            "email": "new@x.com",
            "api_token": "new-token",
            "jira_project_key": "NEW",
            "enabled": True,
        })
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["base_url"] == "https://new.atlassian.net"
        assert data["email"] == "new@x.com"
        assert data["jira_project_key"] == "NEW"
        assert data["enabled"] is True
        assert data["has_token"] is True
        # Token never in response
        assert "api_token" not in data
        assert "api_token_encrypted" not in data

    def test_update_partial_no_token(self, client, jira_project, app):
        """PUT without api_token does not clear the existing encrypted token."""
        client.post(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://a.atlassian.net",
            "email": "a@x.com",
            "api_token": "keep-this-token",
            "jira_project_key": "A",
        })

        # Update only the base_url
        resp = client.put(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://b.atlassian.net",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["base_url"] == "https://b.atlassian.net"
        assert data["has_token"] is True  # token still present

    def test_put_404_no_config(self, client, jira_project):
        """PUT returns 404 if no config exists yet."""
        resp = client.put(f"/api/v1/projects/{jira_project}/jira-config", json={
            "base_url": "https://a.atlassian.net",
        })
        assert resp.status_code == 404


class TestJiraConfigProjectNotFound:
    def test_get_404_unknown_project(self, client):
        """GET returns 404 for unknown project slug."""
        resp = client.get("/api/v1/projects/nonexistent/jira-config")
        assert resp.status_code == 404

    def test_post_404_unknown_project(self, client):
        """POST returns 404 for unknown project slug."""
        resp = client.post("/api/v1/projects/nonexistent/jira-config", json={
            "base_url": "https://a.atlassian.net",
            "email": "a@x.com",
            "api_token": "tok",
            "jira_project_key": "X",
        })
        assert resp.status_code == 404

    def test_put_404_unknown_project(self, client):
        """PUT returns 404 for unknown project slug."""
        resp = client.put("/api/v1/projects/nonexistent/jira-config", json={
            "base_url": "https://a.atlassian.net",
        })
        assert resp.status_code == 404
