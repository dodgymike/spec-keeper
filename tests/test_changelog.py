"""Change-log write-path parity suite (UI-DELTA-3 / UI-DELTA-4).

Every listed UI-relevant mutation must append EXACTLY ONE change entry with an
ascending per-project ``seq``, deletes must be first-class tombstones, and the
whole thing must behave identically on BOTH storage backends. These tests drive
the HTTP API (so the parametrised ``app`` fixture runs each on Postgres AND
DynamoDB) and read the change-log back through ``current_app.storage`` — the
delta HTTP endpoint itself is UI-DELTA-5, so only the storage read path
(``changes_head`` / ``list_changes``) is exercised here.

Atomicity is asserted the observable way: a rejected mutation (stale If-Match ->
412) leaves the head cursor untouched and writes no entry — proving the change
entry rides the mutation's transaction / TransactWriteItems and never leaks on a
rolled-back write.
"""
from __future__ import annotations

BASE = "/api/v1/projects/demo/tasks"
EPICS = "/api/v1/projects/demo/epics"


# --------------------------------------------------------------------------- #
# helpers (read the change-log back through the storage port)
# --------------------------------------------------------------------------- #
def _head(app) -> int:
    with app.app_context():
        return app.storage.changes_head("demo")


def _changes(app, since: int = 0):
    with app.app_context():
        return app.storage.list_changes("demo", since, 1000)


def _mk_task(client, key="T-1", **kw):
    body = {"title": key, "key": key, **kw}
    r = client.post(BASE, json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _one_new_change(app, before, *, entity_type, op):
    """Assert exactly one change was appended since ``before`` and return it."""
    new = _changes(app, since=before)
    assert len(new) == 1, [(c.seq, c.entity_type, c.op) for c in new]
    ch = new[0]
    assert ch.entity_type == entity_type
    assert ch.op == op
    assert ch.seq == before + 1
    return ch


# --------------------------------------------------------------------------- #
# one entry per mutation
# --------------------------------------------------------------------------- #
def test_create_task_emits_one_upsert(client, project, app):
    before = _head(app)
    t = _mk_task(client)
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.entity_pubid == t["public_id"]
    assert ch.version == 1
    assert ch.snapshot["title"] == "T-1"
    assert ch.snapshot["status"] == "todo"
    assert ch.snapshot["version"] == 1


def test_update_task_emits_one_upsert(client, project, app):
    t = _mk_task(client)
    before = _head(app)
    r = client.patch(f"{BASE}/T-1", json={"title": "renamed"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.entity_pubid == t["public_id"]
    assert ch.snapshot["title"] == "renamed"
    assert ch.version == 2


def test_set_status_emits_one_upsert(client, project, app):
    _mk_task(client)
    before = _head(app)
    r = client.post(f"{BASE}/T-1/status", json={"status": "blocked", "note": "waiting"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.snapshot["status"] == "blocked"


def test_release_task_emits_one_upsert(client, project, app):
    _mk_task(client)
    client.post(f"{BASE}/claim-next", json={"agent": "alice"})
    before = _head(app)
    r = client.post(f"{BASE}/T-1/release", json={"reset_to": "todo"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.snapshot["owner"] is None


def test_delete_task_emits_tombstone(client, project, app):
    t = _mk_task(client)
    before = _head(app)
    r = client.delete(f"{BASE}/T-1")
    assert r.status_code == 204
    ch = _one_new_change(app, before, entity_type="task", op="delete")
    assert ch.entity_pubid == t["public_id"]
    assert ch.snapshot is None
    assert ch.version is None


def test_add_commit_emits_one_upsert(client, project, app):
    _mk_task(client)
    before = _head(app)
    r = client.post(f"{BASE}/T-1/commits", json={"sha": "deadbeef"})
    assert r.status_code == 201
    _one_new_change(app, before, entity_type="task", op="upsert")


def test_duplicate_commit_emits_no_change(client, project, app):
    _mk_task(client)
    client.post(f"{BASE}/T-1/commits", json={"sha": "deadbeef"})
    before = _head(app)
    r = client.post(f"{BASE}/T-1/commits", json={"sha": "deadbeef"})
    assert r.status_code == 201
    assert _changes(app, since=before) == []  # dedupe no-op -> no change, no gap


def test_add_relation_emits_one_upsert(client, project, app):
    src = _mk_task(client, key="SRC-1")
    _mk_task(client, key="DST-1")
    before = _head(app)
    r = client.post(f"{BASE}/SRC-1/relations", json={"target": "DST-1", "kind": "blocks"})
    assert r.status_code == 201
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.entity_pubid == src["public_id"]


def test_claim_emits_one_upsert(client, project, app):
    _mk_task(client)
    before = _head(app)
    r = client.post(f"{BASE}/claim-next", json={"agent": "alice"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.snapshot["owner"] == "alice"
    assert ch.snapshot["status"] == "in_progress"


def test_complete_emits_one_upsert(client, project, app):
    _mk_task(client)
    before = _head(app)
    r = client.post(f"{BASE}/T-1/complete", json={"commit_sha": "abc123"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="task", op="upsert")
    assert ch.snapshot["status"] == "done"


def test_task_note_emits_one_upsert(client, project, app):
    _mk_task(client)
    before = _head(app)
    r = client.post(f"{BASE}/T-1/notes", json={"body": "hello", "author": "alice"})
    assert r.status_code == 201
    _one_new_change(app, before, entity_type="task", op="upsert")


def test_create_epic_emits_one_upsert(client, project, app):
    before = _head(app)
    r = client.post(EPICS, json={"key": "E1", "title": "Epic one"})
    assert r.status_code == 201
    ch = _one_new_change(app, before, entity_type="epic", op="upsert")
    assert ch.snapshot["key"] == "E1"
    assert ch.version is None  # epics carry no optimistic-lock version


def test_update_epic_emits_one_upsert(client, project, app):
    client.post(EPICS, json={"key": "E1", "title": "Epic one"})
    before = _head(app)
    r = client.patch(f"{EPICS}/E1", json={"title": "Epic renamed"})
    assert r.status_code == 200
    ch = _one_new_change(app, before, entity_type="epic", op="upsert")
    assert ch.snapshot["title"] == "Epic renamed"


def test_epic_note_emits_one_upsert(client, project, app):
    client.post(EPICS, json={"key": "E1", "title": "Epic one"})
    before = _head(app)
    r = client.post(f"{EPICS}/E1/notes", json={"body": "note", "author": "alice"})
    assert r.status_code == 201
    _one_new_change(app, before, entity_type="epic", op="upsert")


# --------------------------------------------------------------------------- #
# lean snapshot, head cursor, monotonicity, atomicity
# --------------------------------------------------------------------------- #
def test_task_snapshot_is_lean(client, project, app):
    """§6.9: the upsert snapshot carries the scalar TaskOut fields + tags but OMITS
    the nested notes[]/commits[] to bound feed size."""
    _mk_task(client, tags=["x", "y"])
    client.post(f"{BASE}/T-1/commits", json={"sha": "deadbeef"})
    client.post(f"{BASE}/T-1/notes", json={"body": "n", "author": "a"})
    snap = _changes(app)[-1].snapshot
    assert "commits" not in snap and "notes" not in snap
    assert snap["tags"] == ["x", "y"]
    assert {"public_id", "title", "status", "version", "position"} <= set(snap)


def test_changes_head_tracks_max_seq(client, project, app):
    assert _head(app) == 0
    _mk_task(client, key="A")
    _mk_task(client, key="B")
    all_changes = _changes(app)
    assert _head(app) == max(c.seq for c in all_changes) == 2


def test_sequential_mutations_are_contiguous(client, project, app):
    """N sequential mutations -> strictly increasing, contiguous seq (no gaps,
    no dupes) — the monotonic per-project cursor from the atomic counter."""
    _mk_task(client)              # seq 1
    for i in range(6):
        r = client.patch(f"{BASE}/T-1", json={"title": f"v{i}"})
        assert r.status_code == 200
    seqs = [c.seq for c in _changes(app)]
    assert seqs == list(range(1, 8))  # 1..7 contiguous, strictly increasing
    assert _head(app) == 7


def test_rejected_mutation_writes_no_change(client, project, app):
    """A stale If-Match -> 412 leaves the head cursor untouched and appends no
    entry: the change rides the mutation's transaction/TransactWriteItems, so a
    rolled-back mutation can never leak a change (atomicity)."""
    _mk_task(client)
    before = _head(app)
    r = client.patch(f"{BASE}/T-1", json={"title": "no"}, headers={"If-Match": '"v99"'})
    assert r.status_code == 412
    assert _head(app) == before
    assert _changes(app, since=before) == []
