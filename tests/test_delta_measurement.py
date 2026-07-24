"""UI-DELTA-12 — reproducible measurement proof for the delta-loading epic.

This is the *evidence harness* that brackets the UI-DELTA epic: it measures the
**actual serialized HTTP response bytes** of the three loading strategies against a
seeded, backlog-sized project (~120 tasks + a few epics), and asserts the
relationships that prove the win:

  1. **Baseline tick** — the pre-delta ProgressPage refetched the *whole* backlog
     every tick: ``GET /tasks?limit=<all>`` (+ ``GET /epics``). This is the cost we
     are trying to kill.
  2. **Idle tick (new)** — ``GET /changes/head`` is the cheap poll issued every tick
     when nothing has changed (the common case). It must be a rounding error next to
     the baseline.
  3. **Change tick (new)** — after mutating exactly ONE task, ``GET /changes?since=
     <prev cursor>`` propagates just that single change. It must be a small fraction
     of a full refetch.

Marked ``postgres_only``: the numbers measure the **HTTP payload** (Marshmallow ->
JSON), which is backend-independent, so there's no value in running the harness on
both backends — we run it once against Postgres. The behavioural parity of the
underlying endpoints is already proven cross-backend in ``test_changes_api.py`` and
``test_project_heads.py``.

Run it and read the printed report with ``-s``::

    pytest -s -k delta_measurement

The assertions use deliberately generous thresholds (idle < 1% of baseline; a
single-change delta < 25% of baseline) so the proof is not flaky; the *printed*
numbers show the far larger real-world margin.
"""
from __future__ import annotations

import json

import pytest

BASE = "/api/v1/projects/demo/tasks"
EPICS = "/api/v1/projects/demo/epics"
HEAD = "/api/v1/projects/demo/changes/head"
CHANGES = "/api/v1/projects/demo/changes"

N_TASKS = 120
N_EPICS = 5

# A realistic, backlog-sized task description. Real spec-server tasks carry a
# multi-paragraph brief (goal / what-exists / deliverable / chain), so a lean
# placeholder would understate the baseline. This mirrors that shape (~2 KB) so the
# measured per-task cost is comparable to the production backlog (~3 KB/task).
_DESC_TEMPLATE = (
    "Goal: complete task #{i} of the seeded backlog exactly as specified, making the "
    "smallest change that satisfies only this task and nothing else. Restate the task "
    "in one sentence, claim it atomically via claim-next, and never scan-and-pick.\n\n"
    "What exists: the storage abstraction exposes list_tasks / list_epics / changes / "
    "changes_head on both the Postgres reference adapter and the DynamoDB adapter, with "
    "identical observable behaviour (SLS-8 parity). Optimistic locking uses tasks.version "
    "as the ETag; a mutation increments it and honours If-Match with a 412 on mismatch.\n\n"
    "Deliverable: implement the change on BOTH adapters in the same task (no feature lands "
    "on one backend only), keep SQL parameterized and secrets out of tracked files, and "
    "run the narrowest relevant pytest against the throwaway Postgres test database. For "
    "concurrency-touching changes, also run the threaded no-collision tests that prove "
    "claim-next hands each racer a distinct task and reserve_number never repeats a value.\n\n"
    "Chain: spec-keeper -> implementer -> test-engineer -> reviewer -> security -> "
    "documentation; add data-reviewer and reliability-reviewer for any storage-layer change "
    "so adapter parity and failure modes are explicitly checked. Definition of done: the "
    "backlog row is flipped to done via complete with a commit sha, a test summary and a "
    "proof command, AGENT_LOG.md has an appended entry, and git status is clean."
)


def _seed(client):
    """Create N_EPICS epics + N_TASKS backlog-realistic tasks; return () when done."""
    for e in range(N_EPICS):
        r = client.post(
            EPICS,
            json={
                "key": f"EPIC{e}",
                "title": f"Seeded epic {e} for the delta measurement harness",
                "description": (
                    "A representative epic grouping a slice of the seeded backlog so the "
                    "baseline /epics payload is comparable to the real dashboard."
                ),
                "section": "backlog",
            },
        )
        assert r.status_code == 201, r.get_json()

    for i in range(N_TASKS):
        r = client.post(
            BASE,
            json={
                "key": f"SEED-{i}",
                "epic_key": f"EPIC{i % N_EPICS}",
                "title": f"Seeded backlog task #{i}: prove the delta-loading win",
                "description": _DESC_TEMPLATE.format(i=i),
                "priority": ["P0", "P1", "P2", "P3"][i % 4],
                "component": ["storage", "api", "ui", "infra", "auth"][i % 5],
                "proof_cmd": f"pytest -k delta_measurement  # seed row {i}",
                "tags": ["ui-delta", "measurement", f"batch-{i // 20}"],
                "created_by": "test-engineer",
            },
        )
        assert r.status_code == 201, r.get_json()


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


@pytest.mark.postgres_only
def test_delta_loading_measurement_report(client, project, capsys):
    """Measure baseline vs. idle-head vs. single-change bytes and assert the win."""
    _seed(client)

    # ---- 1. Baseline tick: the old "refetch everything" cost -----------------
    tasks_resp = client.get(f"{BASE}?limit=1000")
    assert tasks_resp.status_code == 200, tasks_resp.get_json()
    n_returned = len(tasks_resp.get_json())
    assert n_returned == N_TASKS, f"expected {N_TASKS} tasks, got {n_returned}"

    epics_resp = client.get(EPICS)
    assert epics_resp.status_code == 200, epics_resp.get_json()

    tasks_bytes = len(tasks_resp.data)
    epics_bytes = len(epics_resp.data)
    baseline_bytes = tasks_bytes + epics_bytes

    # ---- 2. Idle tick (new): cheap head poll, nothing changed ----------------
    head_resp = client.get(HEAD)
    assert head_resp.status_code == 200, head_resp.get_json()
    head_body = head_resp.get_json()
    idle_bytes = len(head_resp.data)
    prev_cursor = head_body["cursor"]

    # ---- 3. Change tick (new): mutate ONE task, fetch just that delta --------
    # Optimistic-locking round trip: read version, PATCH with If-Match.
    one = client.get(f"{BASE}/SEED-0").get_json()
    patch = client.patch(
        f"{BASE}/SEED-0",
        json={"status_note": "touched by the UI-DELTA-12 measurement harness"},
        headers={"If-Match": f'"v{one["version"]}"'},
    )
    assert patch.status_code == 200, patch.get_json()

    delta_resp = client.get(f"{CHANGES}?since={prev_cursor}")
    assert delta_resp.status_code == 200, delta_resp.get_json()
    delta_body = delta_resp.get_json()
    assert len(delta_body["changes"]) == 1, delta_body
    assert delta_body["changes"][0]["op"] == "upsert"
    change_bytes = len(delta_resp.data)

    # ---- Ratios --------------------------------------------------------------
    idle_pct = 100.0 * idle_bytes / baseline_bytes
    change_pct = 100.0 * change_bytes / baseline_bytes
    idle_reduction = 100.0 * (1 - idle_bytes / baseline_bytes)
    change_reduction = 100.0 * (1 - change_bytes / baseline_bytes)

    report = (
        "\n"
        "================ UI-DELTA-12 measured results (local) ================\n"
        f"  Seeded project      : {N_TASKS} tasks, {N_EPICS} epics\n"
        "  --------------------------------------------------------------\n"
        f"  BASELINE tick (old) : {baseline_bytes:>8,} B  ({_kb(baseline_bytes)})\n"
        f"      tasks?limit=all : {tasks_bytes:>8,} B  ({_kb(tasks_bytes)}) "
        f"for {n_returned} tasks -> {tasks_bytes / n_returned:,.0f} B/task\n"
        f"      /epics          : {epics_bytes:>8,} B  ({_kb(epics_bytes)})\n"
        "  --------------------------------------------------------------\n"
        f"  IDLE tick (new)     : {idle_bytes:>8,} B  ({_kb(idle_bytes)})  "
        f"= {idle_pct:.4f}% of baseline  ({idle_reduction:.4f}% reduction)\n"
        f"  CHANGE tick (new)   : {change_bytes:>8,} B  ({_kb(change_bytes)})  "
        f"= {change_pct:.2f}% of baseline  ({change_reduction:.2f}% reduction)\n"
        "  --------------------------------------------------------------\n"
        f"  Idle head-poll is  {baseline_bytes / idle_bytes:,.0f}x smaller than a full refetch\n"
        f"  Single-change delta is {baseline_bytes / change_bytes:,.0f}x smaller than a full refetch\n"
        "=====================================================================\n"
    )
    # Emit the report so it shows even when the test passes (pytest -s / -rA).
    with capsys.disabled():
        print(report)

    # ---- Assertions: prove the relationships (generous, non-flaky) ----------
    # Sanity: the seeded backlog is genuinely backlog-sized (not a toy).
    assert baseline_bytes > 100_000, f"baseline unexpectedly small: {baseline_bytes} B"

    # The idle head-poll is a rounding error next to a full refetch: < 1% (the
    # printed number is realistically < 0.1%).
    assert idle_pct < 1.0, f"idle poll not < 1% of baseline: {idle_pct:.4f}%"

    # A single-change delta is a small fraction of a full refetch: < 25% (the
    # printed number is realistically a couple of %).
    assert change_pct < 25.0, f"single-change delta not < 25% of baseline: {change_pct:.2f}%"

    # And the delta must be strictly cheaper than the full refetch it replaces.
    assert change_bytes < baseline_bytes
    assert idle_bytes < change_bytes  # idle poll is cheaper still

    # Guard against a silently-empty measurement.
    assert idle_bytes > 0 and change_bytes > 0

    # Expose the numbers to any caller/CI that imports the captured report.
    _ = json.dumps  # (kept explicit: report is human-facing, asserts are the gate)
