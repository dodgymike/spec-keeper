"""Claim-next: single-claim correctness and no-collision under concurrency."""
from __future__ import annotations

import threading

from app.extensions import db
from app.models import Project, Task, TaskStatus
from app.services import claim_next_task

BASE = "/api/v1/projects/demo/tasks"


def test_claim_next_picks_one_and_marks_in_progress(client, project):
    for i in range(3):
        client.post(BASE, json={"title": f"t{i}", "key": f"K-{i}", "position": i})
    resp = client.post(f"{BASE}/claim-next", json={"agent": "alice"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "in_progress"
    assert data["owner"] == "alice"
    assert data["key"] == "K-0"  # lowest position first


def test_claim_next_empty_returns_204(client, project):
    resp = client.post(f"{BASE}/claim-next", json={"agent": "alice"})
    assert resp.status_code == 204


def test_priority_ordering(client, project):
    client.post(BASE, json={"title": "low", "key": "LO", "priority": "P3"})
    client.post(BASE, json={"title": "high", "key": "HI", "priority": "P0"})
    resp = client.post(f"{BASE}/claim-next", json={"agent": "x"})
    assert resp.get_json()["key"] == "HI"


def test_concurrent_claims_never_collide(app, client, project):
    """N threads claim from N tasks → N distinct tasks, zero double-claims."""
    n = 8
    for i in range(n):
        client.post(BASE, json={"title": f"t{i}", "key": f"C-{i}", "position": i})

    with app.app_context():
        project_id = db.session.execute(
            db.select(Project).where(Project.slug == "demo")
        ).scalar_one().id

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(idx):
        with app.app_context():
            task = claim_next_task(project_id=project_id, agent=f"agent-{idx}")
            db.session.commit()
            if task is not None:
                with lock:
                    claimed.append(task.key)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n
    assert len(set(claimed)) == n  # all distinct — no collisions

    with app.app_context():
        remaining = db.session.execute(
            db.select(db.func.count())
            .select_from(Task)
            .where(Task.status == TaskStatus.todo)
        ).scalar_one()
        assert remaining == 0
