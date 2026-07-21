"""HARDEN epic: expired-lease reaping and pagination."""
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import Lease, LeaseState, Project, Task, TaskStatus, utcnow
from app.services import claim_next_task

BASE = "/api/v1/projects/demo"


@pytest.mark.postgres_only
def test_expired_lease_is_reclaimable(app, client, project):
    client.post(f"{BASE}/tasks", json={"title": "t", "key": "R-1"})
    # alice claims it
    first = client.post(f"{BASE}/tasks/claim-next", json={"agent": "alice"})
    assert first.get_json()["owner"] == "alice"
    # a fresh claim finds nothing (it's leased)
    assert client.post(f"{BASE}/tasks/claim-next", json={"agent": "bob"}).status_code == 204

    # force the lease to expire
    with app.app_context():
        task = db.session.execute(
            sa.select(Task).where(Task.key == "R-1")
        ).scalar_one()
        task.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

    # bob can now reclaim the abandoned task
    second = client.post(f"{BASE}/tasks/claim-next", json={"agent": "bob"})
    assert second.status_code == 200
    data = second.get_json()
    assert data["key"] == "R-1"
    assert data["owner"] == "bob"

    # exactly one active lease remains (the old one was retired)
    with app.app_context():
        project_id = db.session.execute(
            sa.select(Project).where(Project.slug == "demo")
        ).scalar_one().id
        task = db.session.execute(sa.select(Task).where(Task.key == "R-1")).scalar_one()
        active = db.session.execute(
            sa.select(sa.func.count()).select_from(Lease).where(
                Lease.task_id == task.id, Lease.state == LeaseState.active
            )
        ).scalar_one()
        assert active == 1


def test_pagination(client, project):
    for i in range(5):
        client.post(f"{BASE}/tasks", json={"title": f"t{i}", "key": f"P-{i}", "position": i})
    page1 = client.get(f"{BASE}/tasks?limit=2&offset=0").get_json()
    page2 = client.get(f"{BASE}/tasks?limit=2&offset=2").get_json()
    assert [t["key"] for t in page1] == ["P-0", "P-1"]
    assert [t["key"] for t in page2] == ["P-2", "P-3"]
