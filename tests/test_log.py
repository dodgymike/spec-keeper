"""LOG epic: append-only events (auto-emitted + manual) and decisions."""
from __future__ import annotations

BASE = "/api/v1/projects/demo"


def _task(client, key="E-1"):
    return client.post(f"{BASE}/tasks", json={"title": "t", "key": key})


def test_claim_and_complete_emit_events(client, project):
    _task(client, "E-1")
    client.post(f"{BASE}/tasks/claim-next", json={"agent": "alice"})
    client.post(f"{BASE}/tasks/E-1/complete", json={"commit_sha": "abc"})

    events = client.get(f"{BASE}/events").get_json()
    types = [e["event_type"] for e in events]
    assert "claimed" in types
    assert "completed" in types


def test_reserve_emits_event(client, project):
    client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    events = client.get(f"{BASE}/events?event_type=reserved").get_json()
    assert len(events) == 1
    assert events[0]["payload"]["value"] == 1


def test_manual_note_event(client, project):
    resp = client.post(
        f"{BASE}/events",
        json={"event_type": "note", "agent": "bob", "message": "looked into DLQ"},
    )
    assert resp.status_code == 201
    got = client.get(f"{BASE}/events?agent=bob").get_json()
    assert got[0]["message"] == "looked into DLQ"


def test_decision_record(client, project):
    resp = client.post(
        f"{BASE}/decisions",
        json={
            "key": "DEC-1",
            "title": "Use Postgres",
            "decision": "Adopt Postgres for the skip-locked queue.",
            "context": "SQLite cannot express it.",
        },
    )
    assert resp.status_code == 201
    decisions = client.get(f"{BASE}/decisions").get_json()
    assert decisions[0]["title"] == "Use Postgres"
    # recording a decision also emits an event
    evs = client.get(f"{BASE}/events?event_type=decision").get_json()
    assert len(evs) == 1
