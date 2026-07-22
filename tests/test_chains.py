"""LOG-3: chain-run + step tracking."""
from __future__ import annotations

import time

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


def test_list_chain_runs_newest_first(client, project):
    """SLS-12: list a task's runs and the project-wide feed, newest first.

    Cross-backend (Postgres orders by started_at desc + id; Dynamo queries the
    CRUN# item collection and sorts by started_at desc). Sleeps keep the
    microsecond timestamps distinct so ordering is deterministic on both."""
    _task(client, "CH-1")
    _task(client, "CH-2")
    r1 = _start_run(client, "CH-1")
    time.sleep(0.01)
    r2 = _start_run(client, "CH-1")
    time.sleep(0.01)
    r3 = _start_run(client, "CH-2")

    task_runs = client.get(f"{BASE}/tasks/CH-1/chain-runs")
    assert task_runs.status_code == 200, task_runs.get_json()
    ids = [r["public_id"] for r in task_runs.get_json()]
    assert ids == [r2, r1]  # only CH-1's runs, newest first

    all_runs = client.get(f"{BASE}/chain-runs")
    assert all_runs.status_code == 200, all_runs.get_json()
    all_ids = [r["public_id"] for r in all_runs.get_json()]
    assert all_ids == [r3, r2, r1]
    # steps are materialised on the list rows too
    assert all(isinstance(r["steps"], list) for r in all_runs.get_json())

    # limit/offset pagination is bounded and stable (newest-first slice)
    assert [r["public_id"] for r in
            client.get(f"{BASE}/chain-runs?limit=1").get_json()] == [r3]
    assert [r["public_id"] for r in
            client.get(f"{BASE}/chain-runs?limit=1&offset=1").get_json()] == [r2]


def test_chain_run_and_step_emit_events(client, project):
    """SLS-12: creating a run and upserting a step each emit a discoverable
    event (so the activity timeline can surface chain activity), on BOTH
    backends with a consistent kind/shape."""
    _task(client, "CH-1")
    pubid = _start_run(client, "CH-1")
    resp = client.put(
        f"{BASE}/chain-runs/{pubid}/steps/implementer",
        json={"status": "passed", "step_order": 3},
    )
    assert resp.status_code == 200, resp.get_json()

    events = client.get(f"{BASE}/events").get_json()
    by_kind = {e["event_type"]: e for e in events}
    assert "chain_run" in by_kind
    assert "chain_step" in by_kind
    assert by_kind["chain_run"]["payload"].get("run") == pubid
    step_ev = by_kind["chain_step"]
    assert step_ev["payload"].get("run") == pubid
    assert step_ev["payload"].get("step") == "implementer"
    assert step_ev["payload"].get("status") == "passed"
