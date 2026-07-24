"""Delta + head endpoints (UI-DELTA-5) and feed concurrency/parity (UI-DELTA-6).

Drives the HTTP delta feed (``GET /changes``, ``GET /changes/head``) through the
backend-parametrised ``app`` fixture, so every assertion is proven IDENTICALLY on
Postgres AND DynamoDB Local (the SLS-8 parity rule). The feed must be ascending,
gap-free, delete-aware (tombstones), paginate via ``truncated``/``cursor``, expose
a retained-window watermark (``min_retained_seq`` / ``full_resync_required``), and
carry the cursor as an ``ETag`` — on both backends.
"""
from __future__ import annotations

import threading

CHANGES = "/api/v1/projects/demo/changes"
HEAD = "/api/v1/projects/demo/changes/head"
BASE = "/api/v1/projects/demo/tasks"


def _mk_task(client, key):
    r = client.post(BASE, json={"title": key, "key": key})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _seqs(changes):
    return [c["seq"] for c in changes]


# --------------------------------------------------------------------------- #
# Basic delta shape
# --------------------------------------------------------------------------- #
def test_since_zero_returns_all_ascending_gapfree(client, project):
    """After N mutations, ?since=0 returns N entries in ascending, gap-free seq."""
    n = 5
    for i in range(n):
        _mk_task(client, f"T-{i}")

    r = client.get(f"{CHANGES}?since=0")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    seqs = _seqs(body["changes"])
    assert len(seqs) == n
    assert seqs == sorted(seqs)                 # ascending
    assert seqs == list(range(seqs[0], seqs[0] + n))  # contiguous, no gaps
    assert body["cursor"] == seqs[-1]           # cursor == max seq in page
    assert body["truncated"] is False
    assert body["full_resync_required"] is False
    # ETag == the cursor (Option 4).
    assert r.headers["ETag"] == f'"{body["cursor"]}"'


def test_since_head_returns_empty_at_head(client, project):
    """?since=<head> returns an empty page whose cursor is still the head."""
    for i in range(3):
        _mk_task(client, f"T-{i}")
    head = client.get(HEAD).get_json()["cursor"]

    r = client.get(f"{CHANGES}?since={head}")
    body = r.get_json()
    assert body["changes"] == []
    assert body["cursor"] == head               # empty page pins cursor at head
    assert body["truncated"] is False
    assert body["full_resync_required"] is False


def test_limit_smaller_than_backlog_paginates(client, project):
    """A limit below the backlog -> truncated=true; following the cursor drains it."""
    n = 5
    for i in range(n):
        _mk_task(client, f"T-{i}")

    first = client.get(f"{CHANGES}?since=0&limit=2").get_json()
    assert len(first["changes"]) == 2
    assert first["truncated"] is True           # page filled -> more available
    assert _seqs(first["changes"]) == sorted(_seqs(first["changes"]))

    second = client.get(f"{CHANGES}?since={first['cursor']}&limit=2").get_json()
    assert len(second["changes"]) == 2
    assert second["truncated"] is True
    # No overlap and no gap across the page boundary.
    assert second["changes"][0]["seq"] == first["cursor"] + 1

    third = client.get(f"{CHANGES}?since={second['cursor']}&limit=2").get_json()
    assert len(third["changes"]) == 1
    assert third["truncated"] is False          # backlog drained

    # Reassembled feed == a single since=0 read: ascending, gap-free, N entries.
    reassembled = (first["changes"] + second["changes"] + third["changes"])
    seqs = _seqs(reassembled)
    assert len(seqs) == n
    assert seqs == list(range(seqs[0], seqs[0] + n))


def test_delete_appears_as_tombstone(client, project):
    """A delete surfaces as an op=delete entry with a null snapshot."""
    t = _mk_task(client, "T-1")
    head = client.get(HEAD).get_json()["cursor"]
    assert client.delete(f"{BASE}/T-1").status_code == 204

    body = client.get(f"{CHANGES}?since={head}").get_json()
    assert len(body["changes"]) == 1
    ch = body["changes"][0]
    assert ch["op"] == "delete"
    assert ch["entity_pubid"] == t["public_id"]
    assert ch["snapshot"] is None
    assert ch["version"] is None


def test_upsert_carries_snapshot(client, project):
    """An upsert entry carries the entity's snapshot DTO."""
    t = _mk_task(client, "T-1")
    body = client.get(f"{CHANGES}?since=0").get_json()
    ch = body["changes"][-1]
    assert ch["op"] == "upsert"
    assert ch["entity_type"] == "task"
    assert ch["entity_pubid"] == t["public_id"]
    assert isinstance(ch["snapshot"], dict)


# --------------------------------------------------------------------------- #
# Head endpoint
# --------------------------------------------------------------------------- #
def test_head_returns_max_seq(client, project):
    """/changes/head returns the max seq and the retained watermark, + ETag."""
    assert client.get(HEAD).get_json()["cursor"] == 0   # empty project
    for i in range(4):
        _mk_task(client, f"T-{i}")

    r = client.get(HEAD)
    body = r.get_json()
    all_changes = client.get(f"{CHANGES}?since=0").get_json()["changes"]
    assert body["cursor"] == max(_seqs(all_changes))
    assert body["min_retained_seq"] == 0        # nothing pruned
    assert r.headers["ETag"] == f'"{body["cursor"]}"'


# --------------------------------------------------------------------------- #
# Retained-window / full-resync
# --------------------------------------------------------------------------- #
def _prune_below(app, keep_from: int):
    """Simulate a future TTL: drop change entries with seq < keep_from on whichever
    backend is under test, so the retained window starts at ``keep_from``."""
    if app._backend == "postgres":
        import sqlalchemy as sa

        from app.extensions import db
        from app.models import Change, Project
        with app.app_context():
            pid = db.session.execute(
                sa.select(Project.id).where(Project.slug == "demo")
            ).scalar_one()
            db.session.execute(
                sa.delete(Change).where(Change.project_id == pid, Change.seq < keep_from)
            )
            db.session.commit()
    else:
        from app.storage import keys
        table = app._dynamo_table
        for seq in range(1, keep_from):
            table.delete_item(Key={"PK": keys.pk("demo"), "SK": keys.change_sk(seq)})


def test_full_resync_required_when_since_below_watermark(client, project, app):
    """A since below the retained window -> full_resync_required (both backends)."""
    for i in range(6):
        _mk_task(client, f"T-{i}")
    seqs = _seqs(client.get(f"{CHANGES}?since=0").get_json()["changes"])
    keep_from = seqs[3]                          # prune the first three entries

    _prune_below(app, keep_from)

    # Watermark advanced to lowest_retained - 1.
    head = client.get(HEAD).get_json()
    assert head["min_retained_seq"] == keep_from - 1

    # A stale cursor (before the window) is told to full-resync...
    stale = client.get(f"{CHANGES}?since=0").get_json()
    assert stale["full_resync_required"] is True
    assert stale["min_retained_seq"] == keep_from - 1

    # ...while a cursor at/after the watermark is still served incrementally.
    fresh = client.get(f"{CHANGES}?since={keep_from - 1}").get_json()
    assert fresh["full_resync_required"] is False
    assert _seqs(fresh["changes"])[0] == keep_from


# --------------------------------------------------------------------------- #
# Concurrency / parity
# --------------------------------------------------------------------------- #
def test_concurrent_mutations_strictly_increasing_gapfree(app, project):
    """N concurrent mutations -> the feed shows N strictly-increasing seq with no
    gaps and no dupes, on both backends (the atomic seq counter guarantees it)."""
    n = 12
    barrier = threading.Barrier(n)

    def worker(idx):
        c = app.test_client()
        barrier.wait()
        assert c.post(BASE, json={"title": f"t{idx}", "key": f"C-{idx}"}).status_code == 201

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    body = app.test_client().get(f"{CHANGES}?since=0&limit=1000").get_json()
    seqs = _seqs(body["changes"])
    assert len(seqs) == n
    assert len(set(seqs)) == n                  # no dupes
    assert seqs == sorted(seqs)                 # ascending
    assert seqs == list(range(seqs[0], seqs[0] + n))  # strictly +1, gap-free
    assert body["cursor"] == seqs[-1]
