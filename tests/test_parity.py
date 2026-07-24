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
from unittest.mock import patch

import pytest

from app.jira_client import JiraClientError
from app.specmd import ParsedSpec, ParsedTask
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


# --------------------------------------------------------------------------- #
# Jira auto-sync lifecycle parity (SLS-J5)
#
# These go through the HTTP API + current_app.storage, so the parametrised
# ``app`` fixture proves the best-effort Jira auto-sync (wired into create /
# complete + the retry endpoint) behaves IDENTICALLY on BOTH backends. Jira HTTP
# is mocked at the JiraClient boundary; the transition lookup is stubbed so the
# assertions don't depend on a cached-transition warm-up call. Named without the
# ``test_jira_`` prefix on purpose so conftest keeps them CROSS-BACKEND (the
# postgres_only fall-through only tags genuinely ORM-internal ``test_jira_*``).
# --------------------------------------------------------------------------- #
@pytest.fixture
def _jira_key(monkeypatch):
    """Provide the Fernet key so the config's token can be encrypted at rest."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _enable_jira(app, *, enabled=True, project_key="PAR"):
    """Create the ``demo`` project's Jira config via the HTTP API (cross-backend).

    The transition-cache warm-up (which would make a real outbound Jira call on an
    enabled config) is mocked out — this test seeds nothing and stubs
    ``find_transition`` directly where a transition id is needed."""
    c = app.test_client()
    with patch("app.blueprints.jira_config.warm_transition_cache"):
        resp = c.post(f"{PROJ}/jira-config", json={
            "base_url": "https://test.atlassian.net",
            "email": "agent@example.com",
            "api_token": "test-token-not-real",
            "jira_project_key": project_key,
            "enabled": enabled,
        })
    assert resp.status_code == 201, resp.get_json()


def test_autosync_create_sets_issue_key_in_response(app, project, _jira_key):
    """(a) Creating a task with a mocked JiraClient returns the ``jira_issue_key``
    in the TaskOut RESPONSE on BOTH backends — proof the create hook fires through
    the storage lifecycle and the response reflects the write-back."""
    _enable_jira(app)
    c = app.test_client()
    with patch("app.jira_sync.JiraClient.create_issue",
               return_value="PAR-1") as mock_create:
        resp = c.post(BASE, json={"title": "synced task", "key": "JP-1"})

    assert resp.status_code == 201, resp.get_json()
    mock_create.assert_called_once_with(
        project_key="PAR", summary="synced task", description="", issue_type="Task",
    )
    body = resp.get_json()
    assert body["jira_issue_key"] == "PAR-1"   # in the RESPONSE, identically on both
    assert body["jira_sync_error"] is None


def test_autosync_complete_triggers_transition(app, project, _jira_key):
    """(b) Completing a task transitions its Jira issue on BOTH backends."""
    _enable_jira(app)
    c = app.test_client()
    with patch("app.jira_sync.JiraClient.create_issue", return_value="PAR-2"):
        assert c.post(BASE, json={"title": "t", "key": "JP-2"}).status_code == 201

    with patch("app.jira_sync.JiraClient.transition_issue") as mock_transition, \
         patch("app.jira_sync.find_transition",
               return_value={"id": "5", "name": "Done"}):
        resp = c.post(f"{BASE}/JP-2/complete", json={"commit_sha": "abc123"})

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "done"
    mock_transition.assert_called_once_with("PAR-2", "5")


def test_autosync_noop_when_unconfigured(app, project, _jira_key):
    """(c) With NO Jira config, create + complete succeed and make ZERO Jira calls
    on BOTH backends (no outbound call, negligible added latency)."""
    c = app.test_client()
    with patch("app.jira_sync.JiraClient.create_issue") as mock_create, \
         patch("app.jira_sync.JiraClient.transition_issue") as mock_transition:
        r1 = c.post(BASE, json={"title": "plain", "key": "NP-1"})
        assert r1.status_code == 201
        assert r1.get_json()["jira_issue_key"] is None
        r2 = c.post(f"{BASE}/NP-1/complete", json={"commit_sha": "z"})
        assert r2.status_code == 200
        assert r2.get_json()["status"] == "done"

    mock_create.assert_not_called()      # zero outbound Jira calls
    mock_transition.assert_not_called()


def test_autosync_noop_when_disabled(app, project, _jira_key):
    """(c) With a DISABLED Jira config, create + complete succeed and make ZERO
    Jira calls on BOTH backends."""
    _enable_jira(app, enabled=False)
    c = app.test_client()
    with patch("app.jira_sync.JiraClient.create_issue") as mock_create, \
         patch("app.jira_sync.JiraClient.transition_issue") as mock_transition:
        r1 = c.post(BASE, json={"title": "off", "key": "DP-1"})
        assert r1.status_code == 201
        assert r1.get_json()["jira_issue_key"] is None
        r2 = c.post(f"{BASE}/DP-1/complete", json={"commit_sha": "z"})
        assert r2.status_code == 200

    mock_create.assert_not_called()
    mock_transition.assert_not_called()


def test_autosync_retry_clears_prior_error(app, project, _jira_key):
    """(d) The retry endpoint re-runs sync through the storage port and clears a
    prior ``jira_sync_error`` on BOTH backends."""
    _enable_jira(app)
    c = app.test_client()

    # Create with Jira down -> error recorded, no issue key (best-effort; 201).
    with patch("app.jira_sync.JiraClient.create_issue",
               side_effect=JiraClientError(503, "down", "POST", "http://x")):
        r = c.post(BASE, json={"title": "retry me", "key": "RP-1"})
        assert r.status_code == 201
        assert r.get_json()["jira_issue_key"] is None
        assert r.get_json()["jira_sync_error"] is not None

    # Retry with Jira up -> the error is cleared and the issue key is set.
    with patch("app.jira_sync.JiraClient.create_issue",
               return_value="PAR-10") as mock_create:
        resp = c.post(f"{PROJ}/jira/sync")
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["synced"] >= 1
        assert data["failed"] == 0
        mock_create.assert_called()

    got = c.get(f"{BASE}/RP-1").get_json()
    assert got["jira_issue_key"] == "PAR-10"
    assert got["jira_sync_error"] is None


def _tagged_spec():
    """A parsed SPEC.md tree whose tasks carry tags — what ``parse_spec`` yields
    for tagged tasks and what the API create-path already accepts. Rebuilt fresh
    each call so callers can mutate it without cross-test bleed."""
    return ParsedSpec(title="Demo", epics={}, tasks=[
        ParsedTask(key="TAG-1", title="First", section="backlog", position=1.0,
                   tags=["urgent", "backend"]),
        ParsedTask(key="TAG-2", title="Second", section="backlog", position=2.0,
                   tags=["frontend"]),
        ParsedTask(key="TAG-3", title="Third", section="backlog", position=3.0,
                   tags=[]),
    ])


def test_import_persists_task_tags_parity(app, project):
    """PORT-7: ``import_spec`` persists parsed task tags IDENTICALLY on BOTH
    backends. Before the fix, Postgres dropped tags on the create-path while
    DynamoDB kept them (a P1 backend-parity violation). Now, on either backend:
    import stores the parsed tags, export re-emits them (the SPEC.md round-trip
    that was lossy on Postgres only), re-import is a genuine no-op with tags in
    the unchanged-detection, and a tag-only change is detected and applied."""
    st = app.storage

    # ``import_spec``/``get_task`` are called directly (not via HTTP) because the
    # SPEC.md *parser* does not itself re-extract tags; the Postgres adapter needs
    # a Flask app context for its scoped session.
    with app.app_context():
        counts = st.import_spec("demo", _tagged_spec())
        assert counts["tasks_created"] == 3
        assert counts["tasks_updated"] == 0
        assert counts["tasks_unchanged"] == 0

        # stored tags == parsed tags (exactly what Postgres used to drop)
        assert set(st.get_task("demo", "TAG-1").tags) == {"urgent", "backend"}
        assert set(st.get_task("demo", "TAG-2").tags) == {"frontend"}
        assert st.get_task("demo", "TAG-3").tags == []

        # export re-emits the tags — the round-trip that was lossy on Postgres
        # only. Tags render in a deterministic (sorted) order, so the exact meta
        # line is byte-identical on BOTH backends (no association-order drift).
        exported = st.render_spec_text("demo")
        assert "- [ ] TAG-1 · First — backend, urgent" in exported
        assert "- [ ] TAG-2 · Second — frontend" in exported

        # idempotent re-import: tags are in the unchanged-detection -> 0 writes
        counts2 = st.import_spec("demo", _tagged_spec())
        assert counts2["tasks_created"] == 0
        assert counts2["tasks_updated"] == 0
        assert counts2["tasks_unchanged"] == 3
        assert set(st.get_task("demo", "TAG-1").tags) == {"urgent", "backend"}

        # a tag-only change IS detected and rewrites the stored tags on both backends
        changed = _tagged_spec()
        changed.tasks[0].tags = ["urgent", "p0-blocker"]  # dropped "backend", +1
        counts3 = st.import_spec("demo", changed)
        assert counts3["tasks_updated"] == 1
        assert counts3["tasks_unchanged"] == 2
        assert set(st.get_task("demo", "TAG-1").tags) == {"urgent", "p0-blocker"}


def test_event_task_pointer_parity(app, project):
    """UI-DELTA-1: an event's task pointer (``EventOut.task_pubid``) is the task's
    stable ``public_id`` and is populated IDENTICALLY on BOTH backends.

    Before the fix, Postgres dumped the internal integer ``task_id`` while
    DynamoDB hardcoded ``None`` — a hard-parity-rule violation, and the integer
    was never a stable cross-backend pointer. Now, on either backend: an event
    that references a task carries that task's ``public_id``; an event with no
    task carries ``null``."""
    c = app.test_client()

    # A task to point at — capture its stable public_id.
    created = c.post(BASE, json={"title": "pointer target", "key": "EV-1"})
    assert created.status_code == 201, created.get_json()
    public_id = created.get_json()["public_id"]
    assert public_id  # non-empty uuid string

    # An event that references the task -> pointer == that task's public_id.
    ev = c.post(f"{PROJ}/events",
                json={"event_type": "note", "agent": "impl",
                      "message": "touching EV-1", "task_key": "EV-1"})
    assert ev.status_code == 201, ev.get_json()
    body = ev.get_json()
    assert "task_id" not in body  # divergent integer id is gone from the response
    assert body["task_pubid"] == public_id  # identical, non-null, == public_id

    # An event with NO task -> pointer is null (identical on both backends).
    ev_none = c.post(f"{PROJ}/events",
                     json={"event_type": "note", "message": "project-wide"})
    assert ev_none.status_code == 201, ev_none.get_json()
    assert ev_none.get_json()["task_pubid"] is None

    # The list/read path agrees with the write path on both backends.
    listed = c.get(f"{PROJ}/events?task=EV-1").get_json()
    assert [e["task_pubid"] for e in listed] == [public_id]
