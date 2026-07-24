"""Cross-backend parity for the Jira integration-config endpoints (SLS-J3).

Runs against BOTH storage backends (``TEST_BACKENDS=postgres,dynamodb``): the
Jira config now goes through the storage port, so create/get/update, the
Conflict-on-double-create and NotFound-on-update-when-absent semantics, and the
secret-handling guarantee (the plaintext token AND its ciphertext are NEVER in
any response) must be identical on Postgres and DynamoDB.

The token-at-rest-encryption assertion that reaches into the SQLAlchemy ORM stays
in the ``postgres_only`` ``test_jira_config_endpoint`` module. This file is named
without the ``test_jira_`` prefix on purpose so the conftest collection hook does
NOT force it to Postgres-only (see ``pytest_collection_modifyitems``).
"""
from __future__ import annotations

import pytest

SECRET_TOKEN = "super-secret-jira-token-abc123"


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """A per-test Fernet key so ``encrypt()`` works in the blueprint."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def cfg_project(client):
    resp = client.post("/api/v1/projects",
                       json={"slug": "cfg-proj", "name": "Cfg Project"})
    assert resp.status_code == 201, resp.get_json()
    return "cfg-proj"


def _assert_no_token_leak(payload_text: str, data: dict):
    """Neither the plaintext token, nor its ciphertext, nor the token fields
    may ever appear in an API response — on either backend."""
    assert "api_token" not in data
    assert "api_token_encrypted" not in data
    assert SECRET_TOKEN not in payload_text
    assert "gAAAAA" not in payload_text  # Fernet ciphertext prefix


class TestJiraCfgParity:
    def test_create_get_update_roundtrip(self, client, cfg_project):
        base = f"/api/v1/projects/{cfg_project}/jira-config"

        # CREATE (enabled False keeps the path network-free on both backends).
        resp = client.post(base, json={
            "base_url": "https://myco.atlassian.net",
            "email": "agent@myco.com",
            "api_token": SECRET_TOKEN,
            "jira_project_key": "PROJ",
            "enabled": False,
        })
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["has_token"] is True
        assert data["base_url"] == "https://myco.atlassian.net"
        assert data["email"] == "agent@myco.com"
        assert data["jira_project_key"] == "PROJ"
        assert data["enabled"] is False
        _assert_no_token_leak(resp.get_data(as_text=True), data)

        # GET
        resp = client.get(base)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_token"] is True
        assert data["jira_project_key"] == "PROJ"
        _assert_no_token_leak(resp.get_data(as_text=True), data)

        # UPDATE (new token + fields)
        new_token = "rotated-" + SECRET_TOKEN
        resp = client.put(base, json={
            "base_url": "https://new.atlassian.net",
            "email": "new@myco.com",
            "api_token": new_token,
            "jira_project_key": "NEW",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["base_url"] == "https://new.atlassian.net"
        assert data["email"] == "new@myco.com"
        assert data["jira_project_key"] == "NEW"
        assert data["has_token"] is True
        text = resp.get_data(as_text=True)
        _assert_no_token_leak(text, data)
        assert new_token not in text

        # PARTIAL UPDATE keeps the existing token.
        resp = client.put(base, json={"base_url": "https://third.atlassian.net"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["base_url"] == "https://third.atlassian.net"
        assert data["has_token"] is True

    def test_conflict_on_double_create(self, client, cfg_project):
        base = f"/api/v1/projects/{cfg_project}/jira-config"
        payload = {"base_url": "https://a.atlassian.net", "email": "a@x.com",
                   "api_token": SECRET_TOKEN, "jira_project_key": "A"}
        assert client.post(base, json=payload).status_code == 201
        r2 = client.post(base, json=payload)
        assert r2.status_code == 409, r2.get_json()

    def test_notfound_on_update_absent(self, client, cfg_project):
        base = f"/api/v1/projects/{cfg_project}/jira-config"
        r = client.put(base, json={"base_url": "https://a.atlassian.net"})
        assert r.status_code == 404, r.get_json()

    def test_notfound_on_get_absent(self, client, cfg_project):
        base = f"/api/v1/projects/{cfg_project}/jira-config"
        assert client.get(base).status_code == 404

    def test_unknown_project_404(self, client):
        base = "/api/v1/projects/nope/jira-config"
        assert client.get(base).status_code == 404
        assert client.put(base, json={"base_url": "https://a.atlassian.net"}).status_code == 404
        assert client.post(base, json={
            "base_url": "https://a.atlassian.net", "email": "a@x.com",
            "api_token": SECRET_TOKEN, "jira_project_key": "X",
        }).status_code == 404

    def test_set_transitions_via_storage(self, client, cfg_project, app):
        """``set_jira_transitions`` persists cached_transitions on both backends
        and raises NotFound when no config exists (a direct storage-port parity
        check — cached_transitions is intentionally never exposed via the API,
        and the token DTO field stays ciphertext-only)."""
        from app.storage.errors import NotFound
        base = f"/api/v1/projects/{cfg_project}/jira-config"

        with app.app_context():
            with pytest.raises(NotFound):
                app.storage.set_jira_transitions(cfg_project, {"statuses": []})

        assert client.post(base, json={
            "base_url": "https://a.atlassian.net", "email": "a@x.com",
            "api_token": SECRET_TOKEN, "jira_project_key": "A",
        }).status_code == 201

        transitions = {"statuses": [{"id": "1", "name": "To Do"}],
                       "fetched_at": "2026-01-01T00:00:00+00:00"}
        with app.app_context():
            app.storage.set_jira_transitions(cfg_project, transitions)
            cfg = app.storage.get_jira_config(cfg_project)
            assert cfg.cached_transitions == transitions
            # DTO carries ciphertext only, never the plaintext token.
            assert cfg.api_token_encrypted is not None
            assert cfg.api_token_encrypted != SECRET_TOKEN
