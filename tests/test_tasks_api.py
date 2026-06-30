"""API-level tests for task CRUD, completion and optimistic locking."""
from __future__ import annotations

BASE = "/api/v1/projects/demo/tasks"


def _make_task(client, key="T-1", **kw):
    body = {"title": "do a thing", "key": key, **kw}
    return client.post(BASE, json=body)


def test_create_and_get_task(client, project):
    resp = _make_task(client, key="P0-1", priority="P0", component="BE")
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["display_id"] == "P0-1"
    assert data["status"] == "todo"
    assert data["version"] == 1

    get = client.get(f"{BASE}/P0-1")
    assert get.status_code == 200
    assert get.headers["ETag"] == '"v1"'


def test_duplicate_key_conflicts(client, project):
    assert _make_task(client, key="DUP-1").status_code == 201
    assert _make_task(client, key="DUP-1").status_code == 409


def test_complete_flips_to_done(client, project):
    _make_task(client, key="C-1")
    resp = client.post(
        f"{BASE}/C-1/complete",
        json={"commit_sha": "abc123", "test_summary": "5/5 pass"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "done"
    assert data["completed_at"] is not None
    assert data["commits"][0]["sha"] == "abc123"


def test_if_match_conflict_returns_412(client, project):
    _make_task(client, key="L-1")
    # Stale version 0 -> 412
    bad = client.patch(
        f"{BASE}/L-1", json={"title": "new"}, headers={"If-Match": '"v99"'}
    )
    assert bad.status_code == 412

    ok = client.patch(
        f"{BASE}/L-1", json={"title": "new"}, headers={"If-Match": '"v1"'}
    )
    assert ok.status_code == 200
    assert ok.get_json()["version"] == 2


def test_owner_filter_isolates_specs(client, project):
    _make_task(client, key="A-1")
    _make_task(client, key="B-1")
    # claim one for alice
    client.post(f"{BASE}/claim-next", json={"agent": "alice"})
    mine = client.get(f"{BASE}?owner=alice").get_json()
    assert len(mine) == 1
    assert mine[0]["owner"] == "alice"


def test_status_endpoint_sets_blocked(client, project):
    _make_task(client, key="S-1")
    resp = client.post(
        f"{BASE}/S-1/status", json={"status": "blocked", "note": "waiting on RISE"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "blocked"


def test_supersede_relation(client, project):
    _make_task(client, key="OLD-1")
    _make_task(client, key="NEW-1")
    client.post(
        f"{BASE}/NEW-1/relations", json={"target": "OLD-1", "kind": "supersedes"}
    )
    old = client.get(f"{BASE}/OLD-1").get_json()
    assert old["status"] == "superseded"
