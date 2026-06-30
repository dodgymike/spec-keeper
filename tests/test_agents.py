"""Agent registry is per-project: two projects can hold the same agent slug."""
from __future__ import annotations


def _project(client, slug):
    client.post("/api/v1/projects", json={"slug": slug, "name": slug})


def test_register_and_list_scoped_to_project(client, project):
    # `project` fixture created slug "demo".
    r = client.post("/api/v1/projects/demo/agents",
                    json={"slug": "spec-keeper", "display_name": "Spec Keeper"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["slug"] == "spec-keeper"
    assert body["project"] == "demo"

    listing = client.get("/api/v1/projects/demo/agents").get_json()
    assert [a["slug"] for a in listing] == ["spec-keeper"]


def test_same_slug_in_two_projects(client, project):
    _project(client, "other")
    a = client.post("/api/v1/projects/demo/agents", json={"slug": "spec-keeper"})
    b = client.post("/api/v1/projects/other/agents", json={"slug": "spec-keeper"})
    assert a.status_code == 201 and b.status_code == 201
    # Each project sees only its own roster.
    assert len(client.get("/api/v1/projects/demo/agents").get_json()) == 1
    assert len(client.get("/api/v1/projects/other/agents").get_json()) == 1


def test_register_is_idempotent(client, project):
    client.post("/api/v1/projects/demo/agents", json={"slug": "reviewer"})
    client.post("/api/v1/projects/demo/agents",
                json={"slug": "reviewer", "display_name": "updated"})
    listing = client.get("/api/v1/projects/demo/agents").get_json()
    assert len(listing) == 1
    assert listing[0]["display_name"] == "updated"


def test_register_unknown_project_404(client):
    r = client.post("/api/v1/projects/nope/agents", json={"slug": "x"})
    assert r.status_code == 404
