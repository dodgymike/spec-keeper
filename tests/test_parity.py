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

from app.storage.errors import Conflict

BASE = "/api/v1/projects/demo/tasks"
PROJ = "/api/v1/projects/demo"


def test_create_project_duplicate_slug_conflict(app, project):
    """A duplicate slug returns an identical 409 on BOTH backends (ISO-8).

    Sequential case: the ``demo`` slug already exists, so a second create collides
    and both adapters raise the same Conflict -> 409 with the same message. No
    duplicate/partial project row is left behind."""
    c = app.test_client()
    resp = c.post("/api/v1/projects", json={"slug": "demo", "name": "Other"})
    assert resp.status_code == 409, resp.get_json()
    assert "already exists" in resp.get_json()["message"]

    demos = [p for p in c.get("/api/v1/projects").get_json() if p["slug"] == "demo"]
    assert len(demos) == 1
    assert demos[0]["name"] == "Demo"  # loser did not overwrite the winner


def test_concurrent_duplicate_slug_single_winner(app):
    """N threads racing to create the SAME slug -> exactly one 201, the rest 409,
    NEVER a 500 — identical on both backends (ISO-8).

    This is the real parity proof: concurrency defeats Postgres' racy pre-check
    SELECT so multiple threads reach the UNIQUE(projects.slug) constraint at
    COMMIT. Before ISO-8 the losers surfaced an uncaught IntegrityError -> HTTP
    500 (DynamoDB always returned 409); now Postgres catches it, rolls back, and
    maps it to the same Conflict -> 409."""
    n = 8
    results: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        c = app.test_client()
        r = c.post("/api/v1/projects", json={"slug": "race", "name": f"R{idx}"})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201] + [409] * (n - 1), results
    # Exactly one surviving project row for the contested slug.
    races = [p for p in app.test_client().get("/api/v1/projects").get_json()
             if p["slug"] == "race"]
    assert len(races) == 1


def test_concurrent_duplicate_slug_no_orphan_member(app):
    """The creator-admin path (ISO-4) rolls back cleanly under a racing duplicate:
    the losing creates leave NO orphaned member and NO half-created project, on
    both backends (ISO-8).

    Driven through app.storage with a per-thread ``creator_sub`` so every racer
    also attempts the member write; only the winner's project + its single admin
    member must survive."""
    n = 6
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(idx):
        with app.app_context():
            try:
                app.storage.create_project(
                    {"slug": "team", "name": f"T{idx}"},
                    creator_sub=f"sub-{idx}", creator_name=f"Owner{idx}")
                with lock:
                    outcomes.append(f"win-{idx}")
            except Conflict:
                with lock:
                    outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [o for o in outcomes if o.startswith("win-")]
    assert len(wins) == 1, outcomes
    assert outcomes.count("conflict") == n - 1, outcomes

    winner_sub = f"sub-{wins[0].split('-')[1]}"
    with app.app_context():
        members = app.storage.list_members("team")
        # Exactly the winner's admin member — no orphans from the rolled-back racers.
        assert [m.principal_sub for m in members] == [winner_sub], \
            [m.principal_sub for m in members]


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
