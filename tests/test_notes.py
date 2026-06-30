"""Task notes: add timestamped comments and read them back."""
from __future__ import annotations

BASE = "/api/v1/projects/demo/tasks"


def _task(client, key="N-1"):
    client.post(BASE, json={"title": "t", "key": key})


def test_add_and_list_notes(client, project):
    _task(client, "N-1")
    r = client.post(f"{BASE}/N-1/notes",
                    json={"body": "looked into the DLQ; root cause is X", "author": "alice"})
    assert r.status_code == 201
    assert r.get_json()["author"] == "alice"

    client.post(f"{BASE}/N-1/notes", json={"body": "second note"})
    notes = client.get(f"{BASE}/N-1/notes").get_json()
    assert [n["body"] for n in notes] == [
        "looked into the DLQ; root cause is X", "second note"
    ]
    assert notes[0]["created_at"] is not None


def test_notes_appear_on_the_task(client, project):
    _task(client, "N-2")
    client.post(f"{BASE}/N-2/notes", json={"body": "a note"})
    task = client.get(f"{BASE}/N-2").get_json()
    assert [n["body"] for n in task["notes"]] == ["a note"]


def test_note_requires_body(client, project):
    _task(client, "N-3")
    assert client.post(f"{BASE}/N-3/notes", json={"author": "x"}).status_code == 422


def test_adding_a_note_emits_an_event(client, project):
    _task(client, "N-4")
    client.post(f"{BASE}/N-4/notes", json={"body": "hello", "author": "bob"})
    evs = client.get("/api/v1/projects/demo/events?event_type=note").get_json()
    assert any("hello" in e["message"] for e in evs)


def test_project_wide_notes_listing_and_filters(client, project):
    _task(client, "N-5")
    _task(client, "N-6")
    client.post(f"{BASE}/N-5/notes", json={"body": "alpha", "author": "alice"})
    client.post(f"{BASE}/N-6/notes", json={"body": "beta", "author": "bob"})
    client.post(f"{BASE}/N-6/notes", json={"body": "gamma", "author": "alice"})

    P = "/api/v1/projects/demo/notes"
    # all notes, newest first, each carries its task
    alln = client.get(P).get_json()
    assert len(alln) == 3
    assert alln[0]["body"] == "gamma"            # newest first
    assert {n["task"] for n in alln} == {"N-5", "N-6"}

    # filter by author
    alice = client.get(f"{P}?author=alice").get_json()
    assert sorted(n["body"] for n in alice) == ["alpha", "gamma"]

    # filter by task
    n6 = client.get(f"{P}?task=N-6").get_json()
    assert sorted(n["body"] for n in n6) == ["beta", "gamma"]
