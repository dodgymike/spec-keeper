"""Idempotency-Key support (HARDEN-3): retries replay, they don't re-execute."""
from __future__ import annotations

BASE = "/api/v1/projects/demo"


def test_reserve_idempotent(client, project):
    """A repeated POST with the same Idempotency-Key returns the original value
    instead of allocating a new number; omitting the header still increments."""
    hdr = {"Idempotency-Key": "k1"}
    body = {"namespace": "migration"}

    first = client.post(f"{BASE}/reservations", json=body, headers=hdr)
    assert first.status_code == 201
    assert first.get_json()["value"] == 1

    # Same key + body: replayed, NOT a new reservation.
    second = client.post(f"{BASE}/reservations", json=body, headers=hdr)
    assert second.get_json()["value"] == 1

    # No header: the normal path still increments.
    third = client.post(f"{BASE}/reservations", json=body)
    assert third.get_json()["value"] == 2


def test_claim_idempotent(client, project):
    """Retrying claim-next with the same key returns the SAME task and does not
    consume a second one; a keyless claim then gets the other task."""
    tasks = f"{BASE}/tasks"
    client.post(tasks, json={"title": "a1", "key": "A-1", "position": 0})
    client.post(tasks, json={"title": "a2", "key": "A-2", "position": 1})

    hdr = {"Idempotency-Key": "c1"}
    first = client.post(f"{tasks}/claim-next", json={"agent": "alice"}, headers=hdr)
    assert first.status_code == 200
    first_key = first.get_json()["key"]

    # Same key: replays the SAME task, does not consume a second.
    second = client.post(f"{tasks}/claim-next", json={"agent": "alice"}, headers=hdr)
    assert second.get_json()["key"] == first_key

    # A keyless claim by another agent gets the OTHER task — proving the
    # idempotent retries only consumed one.
    third = client.post(f"{tasks}/claim-next", json={"agent": "bob"})
    assert third.status_code == 200
    assert third.get_json()["key"] != first_key
    assert {first_key, third.get_json()["key"]} == {"A-1", "A-2"}
