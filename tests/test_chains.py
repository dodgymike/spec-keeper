"""LOG-3: chain-run + step tracking."""
from __future__ import annotations

BASE = "/api/v1/projects/demo"


def _task(client, key="CH-1"):
    return client.post(f"{BASE}/tasks", json={"title": "t", "key": key})


def _start_run(client, key="CH-1"):
    resp = client.post(f"{BASE}/tasks/{key}/chain-runs", json={})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["public_id"]


def test_start_run_and_get(client, project):
    _task(client, "CH-1")
    resp = client.post(f"{BASE}/tasks/CH-1/chain-runs", json={"started_by": "alice"})
    assert resp.status_code == 201, resp.get_json()
    pubid = resp.get_json()["public_id"]

    got = client.get(f"{BASE}/chain-runs/{pubid}")
    assert got.status_code == 200
    assert got.get_json()["status"] == "running"


def test_set_step_passed(client, project):
    _task(client, "CH-1")
    pubid = _start_run(client, "CH-1")

    resp = client.put(
        f"{BASE}/chain-runs/{pubid}/steps/implementer",
        json={"status": "passed", "step_order": 3},
    )
    assert resp.status_code == 200, resp.get_json()

    run = client.get(f"{BASE}/chain-runs/{pubid}").get_json()
    steps = {s["step_name"]: s for s in run["steps"]}
    assert "implementer" in steps
    assert steps["implementer"]["status"] == "passed"
    assert steps["implementer"]["step_order"] == 3


def test_skip_requires_justification(client, project):
    _task(client, "CH-1")
    pubid = _start_run(client, "CH-1")

    bad = client.put(
        f"{BASE}/chain-runs/{pubid}/steps/security",
        json={"status": "skipped"},
    )
    assert bad.status_code == 422, bad.get_json()

    ok = client.put(
        f"{BASE}/chain-runs/{pubid}/steps/security",
        json={"status": "skipped", "skip_justification": "trivial doc change"},
    )
    assert ok.status_code == 200, ok.get_json()


def test_finish_run(client, project):
    _task(client, "CH-1")
    pubid = _start_run(client, "CH-1")

    resp = client.patch(f"{BASE}/chain-runs/{pubid}", json={"status": "passed"})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["status"] == "passed"
    assert body["finished_at"] is not None


def test_list_chain_runs_for_task(client, project):
    _task(client, "CH-1")
    first = _start_run(client, "CH-1")
    second = _start_run(client, "CH-1")

    resp = client.get(f"{BASE}/tasks/CH-1/chain-runs")
    assert resp.status_code == 200, resp.get_json()
    runs = resp.get_json()
    assert len(runs) == 2
    assert {r["public_id"] for r in runs} == {first, second}
    # oldest first
    assert [r["public_id"] for r in runs] == [first, second]


def test_list_chain_runs_empty_when_none(client, project):
    _task(client, "CH-2")
    resp = client.get(f"{BASE}/tasks/CH-2/chain-runs")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_chain_runs_404_unknown_task(client, project):
    resp = client.get(f"{BASE}/tasks/NOPE-1/chain-runs")
    assert resp.status_code == 404
