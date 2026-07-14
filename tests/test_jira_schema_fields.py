"""Tests for JIRA-12: jira_issue_key and jira_sync_error exposed on task schema (dump-only)."""
from __future__ import annotations

from app.extensions import db
from app.models import Project, Task, TaskStatus

BASE = "/api/v1/projects/demo/tasks"


def _make_task(client, key="J12-1", **kw):
    body = {"title": "jira schema test", "key": key, **kw}
    return client.post(BASE, json=body)


class TestJiraFieldsInResponse:
    """The two jira fields appear in task GET/POST responses (null by default)."""

    def test_create_task_response_includes_jira_fields(self, client, project):
        resp = _make_task(client)
        assert resp.status_code == 201
        data = resp.get_json()
        assert "jira_issue_key" in data
        assert "jira_sync_error" in data
        assert data["jira_issue_key"] is None
        assert data["jira_sync_error"] is None

    def test_get_task_response_includes_jira_fields(self, client, project):
        _make_task(client, key="J12-2")
        resp = client.get(f"{BASE}/J12-2")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "jira_issue_key" in data
        assert "jira_sync_error" in data
        assert data["jira_issue_key"] is None
        assert data["jira_sync_error"] is None

    def test_get_task_with_jira_values_set(self, client, app, project):
        """If the model has jira values set (by server-side logic), they appear in output."""
        _make_task(client, key="J12-3")
        # Set jira fields directly on the model (simulating sync service)
        with app.app_context():
            task = db.session.query(Task).filter(Task.key == "J12-3").one()
            task.jira_issue_key = "MYPROJ-42"
            task.jira_sync_error = "rate limited"
            db.session.commit()

        resp = client.get(f"{BASE}/J12-3")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["jira_issue_key"] == "MYPROJ-42"
        assert data["jira_sync_error"] == "rate limited"


class TestJiraFieldsDumpOnly:
    """Clients cannot set jira_issue_key or jira_sync_error via input."""

    def test_create_task_rejects_jira_issue_key_in_body(self, client, project):
        """jira_issue_key is not in TaskIn, so flask-smorest rejects it as unknown."""
        resp = client.post(BASE, json={
            "title": "sneaky create",
            "key": "J12-4",
            "jira_issue_key": "HACKED-1",
        })
        # The input schema (TaskIn) does NOT include jira_issue_key;
        # flask-smorest raises 422 for unknown fields — proving dump-only enforcement.
        assert resp.status_code == 422
        errors = resp.get_json().get("errors", {}).get("json", {})
        assert "jira_issue_key" in errors

    def test_create_task_rejects_jira_sync_error_in_body(self, client, project):
        """jira_sync_error is not in TaskIn, so flask-smorest rejects it as unknown."""
        resp = client.post(BASE, json={
            "title": "sneaky create 2",
            "key": "J12-5",
            "jira_sync_error": "injected error",
        })
        assert resp.status_code == 422
        errors = resp.get_json().get("errors", {}).get("json", {})
        assert "jira_sync_error" in errors

    def test_patch_task_ignores_jira_fields(self, client, project):
        _make_task(client, key="J12-6")
        resp = client.patch(f"{BASE}/J12-6", json={
            "title": "updated title",
            "jira_issue_key": "HACKED-2",
            "jira_sync_error": "injected",
        })
        # PATCH should succeed (unknown fields ignored by marshmallow unknown=EXCLUDE)
        # or rejected (unknown=RAISE) — either way the jira fields must NOT be set
        if resp.status_code == 200:
            data = resp.get_json()
            assert data["jira_issue_key"] is None
            assert data["jira_sync_error"] is None
        elif resp.status_code == 422:
            # If marshmallow is strict, the fields are rejected, which also satisfies dump-only
            pass
        else:
            raise AssertionError(f"Unexpected status {resp.status_code}: {resp.get_json()}")


class TestJiraFieldsInOpenAPI:
    """The OpenAPI spec includes jira_issue_key and jira_sync_error on the TaskOut schema."""

    def test_openapi_includes_jira_fields(self, client, app):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.get_json()
        # Find the TaskOut schema in components/schemas
        schemas = spec.get("components", {}).get("schemas", {})
        # The schema name could be "TaskOut" or "Task" — find the one with task fields
        task_schema = None
        for name, schema in schemas.items():
            props = schema.get("properties", {})
            if "jira_issue_key" in props:
                task_schema = schema
                break

        assert task_schema is not None, (
            f"No schema with jira_issue_key found. Available schemas: {list(schemas.keys())}"
        )
        props = task_schema["properties"]
        assert "jira_issue_key" in props
        assert "jira_sync_error" in props
        # Verify they are marked as readOnly (dump_only -> readOnly in OpenAPI)
        assert props["jira_issue_key"].get("readOnly") is True
        assert props["jira_sync_error"].get("readOnly") is True
