"""Health probe tests (SLS-14): /readyz is backend-aware, /healthz is static."""
from __future__ import annotations

from app.storage.errors import BackendUnavailable


def test_healthz_is_static_liveness(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_readyz_ready_on_any_backend(client):
    """/readyz delegates to current_app.storage.ping(); it must be 200 on both
    the Postgres and DynamoDB backends (the parametrised `client` fixture runs
    both), proving it is no longer Postgres-hardcoded."""
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ready"}


def test_readyz_unready_when_backend_ping_fails(client, monkeypatch):
    """When the storage backend's ping() raises, /readyz reports 503 unready with
    the error surfaced -- same wire shape as before, backend-neutral now."""

    def boom():
        raise BackendUnavailable("backend down")

    app = client.application
    monkeypatch.setattr(app.storage, "ping", boom)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "unready"
    assert "backend down" in body["error"]
