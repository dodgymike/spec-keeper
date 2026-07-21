"""Cross-backend parity suite (SLS-8).

These tests go through the HTTP API only (``current_app.storage`` under the
hood), so the parametrised ``app`` fixture runs every one against BOTH the
Postgres and DynamoDB adapters. They are the real proof that the two atomic
guarantees + the optimistic-lock contract hold identically on each backend:

* no-collision claim across N threads,
* collision-proof CONTIGUOUS reservation across N threads,
* complete -> done,
* If-Match -> 412,
* expired-lease reclaim.

The pure-ORM versions (driving ``db.session`` directly) live in
``test_claim.py`` / ``test_reservations.py`` / ``test_harden.py`` marked
``postgres_only``; these are their backend-neutral equivalents.
"""
from __future__ import annotations

import threading
import time

BASE = "/api/v1/projects/demo/tasks"
PROJ = "/api/v1/projects/demo"


def test_no_collision_claim_across_threads(app, project):
    """N threads claim from N tasks -> N distinct tasks, zero double-claims,
    zero todo left. The conditional UpdateItem (Dynamo) / SKIP LOCKED (PG) guard
    makes a double-claim impossible on either backend."""
    n = 8
    c0 = app.test_client()
    for i in range(n):
        assert c0.post(BASE, json={"title": f"t{i}", "key": f"C-{i}",
                                   "position": i}).status_code == 201

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(idx):
        c = app.test_client()
        resp = c.post(f"{BASE}/claim-next", json={"agent": f"agent-{idx}"})
        if resp.status_code == 200:
            with lock:
                claimed.append(resp.get_json()["key"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n
    assert len(set(claimed)) == n  # all distinct — no collisions

    remaining = c0.get(f"{BASE}?status=todo").get_json()
    assert remaining == []


def test_contiguous_reservation_across_threads(app, project):
    """N threads reserving the same namespace get N distinct, CONTIGUOUS values
    (1..N) — the 'two agents both grabbed 024' bug is impossible on either
    backend (atomic ADD / ON CONFLICT + UNIQUE backstop)."""
    n = 20
    values: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        c = app.test_client()
        resp = c.post(f"{PROJ}/reservations",
                      json={"namespace": "migration", "reserved_by": f"a{idx}"})
        assert resp.status_code == 201, resp.get_json()
        with lock:
            values.append(resp.get_json()["value"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(values) == n
    assert sorted(values) == list(range(1, n + 1))  # distinct & contiguous


def test_complete_flips_to_done(app, project):
    c = app.test_client()
    c.post(BASE, json={"title": "t", "key": "D-1"})
    resp = c.post(f"{BASE}/D-1/complete",
                  json={"commit_sha": "abc123", "test_summary": "5/5 pass"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "done"
    assert data["completed_at"] is not None
    assert data["commits"][0]["sha"] == "abc123"
    assert data["owner"] is None


def test_if_match_optimistic_lock(app, project):
    c = app.test_client()
    c.post(BASE, json={"title": "t", "key": "L-1"})
    bad = c.patch(f"{BASE}/L-1", json={"title": "new"},
                  headers={"If-Match": '"v99"'})
    assert bad.status_code == 412
    ok = c.patch(f"{BASE}/L-1", json={"title": "new"},
                 headers={"If-Match": '"v1"'})
    assert ok.status_code == 200
    assert ok.get_json()["version"] == 2


def test_expired_lease_reclaim(app, project):
    """A claimed-but-expired task returns to the pool and is reclaimable — the
    lazy-reclaim path, identical behaviour on both backends."""
    c = app.test_client()
    c.post(BASE, json={"title": "t", "key": "R-1"})
    first = c.post(f"{BASE}/claim-next", json={"agent": "alice", "lease_ttl": 1})
    assert first.get_json()["owner"] == "alice"
    # a fresh claim finds nothing (still leased)
    assert c.post(f"{BASE}/claim-next", json={"agent": "bob"}).status_code == 204
    time.sleep(1.5)  # let the lease expire
    second = c.post(f"{BASE}/claim-next", json={"agent": "bob"})
    assert second.status_code == 200
    body = second.get_json()
    assert body["key"] == "R-1"
    assert body["owner"] == "bob"
