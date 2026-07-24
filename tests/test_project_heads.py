"""Batched fan-out head map (UI-DELTA-10): ``GET /api/v1/projects/heads``.

One request returns the change-log head (``cursor`` + ``min_retained_seq``) for
each of the caller's VISIBLE projects, replacing the N-request fan-out of polling
``/changes/head`` once per project on a multi-project dashboard.

Everything here runs through the backend-parametrised ``app`` fixture, so the map
is proven IDENTICAL on Postgres AND DynamoDB Local (the SLS-8 parity rule): each
slug's value must equal that project's single ``/changes/head`` response, and the
map must advance after a mutation. (Isolation-scoping — a non-member's head is
absent — is proven in ``test_isolation.py`` where the Cognito-auth harness lives.)
"""
from __future__ import annotations

HEADS = "/api/v1/projects/heads"


def _mk_project(client, slug):
    r = client.post("/api/v1/projects", json={"slug": slug, "name": slug})
    assert r.status_code == 201, r.get_json()
    return slug


def _mk_task(client, slug, key):
    r = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": key, "key": key})
    assert r.status_code == 201, r.get_json()


def _head(client, slug):
    return client.get(f"/api/v1/projects/{slug}/changes/head").get_json()


def test_heads_map_matches_per_project_head(client):
    """Each slug's {cursor, min_retained_seq} in the batch equals that project's
    own /changes/head — for several projects with different change counts."""
    _mk_project(client, "alpha")
    _mk_project(client, "beta")
    _mk_project(client, "gamma")  # gamma stays empty -> cursor 0
    for i in range(3):
        _mk_task(client, "alpha", f"A-{i}")
    _mk_task(client, "beta", "B-0")

    heads = client.get(HEADS).get_json()["heads"]
    assert set(heads) == {"alpha", "beta", "gamma"}
    for slug in ("alpha", "beta", "gamma"):
        assert heads[slug] == _head(client, slug), slug
    # Sanity: the head reflects the mutation counts (gamma untouched == 0).
    assert heads["alpha"]["cursor"] > heads["beta"]["cursor"] > heads["gamma"]["cursor"] == 0
    assert heads["gamma"]["min_retained_seq"] == 0


def test_empty_project_present_with_zero_cursor(client):
    """A project that has never been mutated is PRESENT in the map with cursor 0
    (not omitted) — mirrors its own /changes/head returning 0."""
    _mk_project(client, "solo")
    heads = client.get(HEADS).get_json()["heads"]
    assert heads["solo"] == {"cursor": 0, "min_retained_seq": 0}
    assert heads["solo"] == _head(client, "solo")


def test_heads_advances_after_mutation(client):
    """The batched head for a project advances after a mutation and keeps matching
    that project's /changes/head cursor."""
    _mk_project(client, "proj")
    before = client.get(HEADS).get_json()["heads"]["proj"]["cursor"]
    assert before == 0

    _mk_task(client, "proj", "T-1")
    after_map = client.get(HEADS).get_json()["heads"]["proj"]
    assert after_map["cursor"] > before
    assert after_map == _head(client, "proj")

    # A second mutation advances it again, still matching the single endpoint.
    _mk_task(client, "proj", "T-2")
    latest = client.get(HEADS).get_json()["heads"]["proj"]
    assert latest["cursor"] > after_map["cursor"]
    assert latest == _head(client, "proj")


def test_no_projects_returns_empty_map(client):
    """With no projects the batch is an empty map (not an error)."""
    assert client.get(HEADS).get_json()["heads"] == {}
