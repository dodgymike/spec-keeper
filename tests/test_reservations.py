"""Reservations: sequential allocation and collision-proof concurrency."""
from __future__ import annotations

import threading

import pytest

from app.extensions import db
from app.models import Project
from app.services import reserve_number

BASE = "/api/v1/projects/demo"


def test_reserve_increments_sequentially(client, project):
    v1 = client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    v2 = client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    assert v1.get_json()["value"] == 1
    assert v2.get_json()["value"] == 2


def test_namespaces_are_independent(client, project):
    a = client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    b = client.post(f"{BASE}/reservations", json={"namespace": "queue"})
    assert a.get_json()["value"] == 1
    assert b.get_json()["value"] == 1


def test_counters_endpoint(client, project):
    client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    client.post(f"{BASE}/reservations", json={"namespace": "migration"})
    counters = client.get(f"{BASE}/counters").get_json()
    assert {"namespace": "migration", "current_value": 2} in counters


@pytest.mark.postgres_only
def test_concurrent_reservations_are_collision_proof(app, client, project):
    """The 'two agents both grabbed 024' bug must be impossible: N threads
    reserving the same namespace get N distinct, contiguous values.

    Postgres-specific: drives ``reserve_number``/``db.session`` directly. The
    cross-backend equivalent (via the HTTP API) is in ``test_parity.py``."""
    n = 20
    with app.app_context():
        project_id = db.session.execute(
            db.select(Project).where(Project.slug == "demo")
        ).scalar_one().id

    values: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        with app.app_context():
            res = reserve_number(project_id, "migration", reserved_by=f"a{idx}")
            db.session.commit()
            with lock:
                values.append(res.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(values) == n
    assert sorted(values) == list(range(1, n + 1))  # distinct & contiguous
