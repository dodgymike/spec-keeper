"""PORT epic: SPEC.md parse / import / export / round-trip / diff."""
from __future__ import annotations

from app.specmd import normalize, parse_spec, render_spec

SAMPLE = """# Demo Project

> Checkbox legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` superseded.

## In Progress

### EPIC FOUND — Foundations

- [x] **FOUND-1 · Scaffold the repo** (BE). Set up the package.
  _Proof: python -c "import app"_
- [~] FOUND-2 · Dockerfile and compose (infra).

## To Do

### EPIC API — REST surface

- [ ] API-1 · Bearer auth (BE, P0). Resolve the agent.
  _Proof: pytest -k auth_
- [ ] API-2 · Tasks CRUD (BE, P1).
- [-] API-3 · Legacy endpoint (superseded).

## Completed

- [x] CHORE-1 · Pick a license.
"""


def test_parse_extracts_structure():
    spec = parse_spec(SAMPLE)
    assert spec.title == "Demo Project"
    assert set(spec.epics) == {"FOUND", "API"}
    by_key = {t.key: t for t in spec.tasks}

    assert by_key["FOUND-1"].status == "done"
    assert by_key["FOUND-1"].component == "BE"
    assert by_key["FOUND-1"].proof_cmd == 'python -c "import app"'
    assert by_key["FOUND-1"].epic_key == "FOUND"
    assert by_key["FOUND-1"].section == "in_progress"

    assert by_key["FOUND-2"].status == "in_progress"
    assert by_key["API-1"].priority == "P0"
    assert by_key["API-1"].section == "to_do"
    assert by_key["API-3"].status == "superseded"
    assert by_key["CHORE-1"].section == "completed"
    assert by_key["CHORE-1"].epic_key is None


def test_roundtrip_fidelity():
    """parse -> render -> parse preserves the normalized tree (PORT-4)."""
    first = parse_spec(SAMPLE)

    class _T:
        def __init__(self, t):
            for f in ("key", "title", "description", "status", "priority",
                      "component", "proof_cmd", "section", "position", "epic_key"):
                setattr(self, f, getattr(t, f))
            self.tag_keys = []

    rendered = render_spec("Demo Project", list(first.epics.values()),
                           [_T(t) for t in first.tasks])
    second = parse_spec(rendered)
    assert normalize(first) == normalize(second)


def test_import_export_via_api(client, project):
    B = "/api/v1/projects/demo"
    imp = client.post(f"{B}/import", data=SAMPLE,
                      headers={"Content-Type": "text/markdown"})
    assert imp.status_code == 200
    assert "created" in imp.get_json()["message"]

    # PORT-6: the import returns a structured result the agent can self-verify.
    body1 = imp.get_json()
    assert body1["total"] == 6
    assert body1["created"] == 6
    assert body1["updated"] == 0 and body1["unchanged"] == 0
    assert body1["failed"] == []

    # idempotent: second import creates no new tasks (all unchanged -> no writes)
    imp2 = client.post(f"{B}/import", data=SAMPLE,
                       headers={"Content-Type": "text/markdown"})
    body2 = imp2.get_json()
    assert "0 task(s) created" in body2["message"]
    assert body2["created"] == 0
    assert body2["unchanged"] == 6
    assert body2["total"] == 6

    tasks = client.get(f"{B}/tasks").get_json()
    assert len(tasks) == 6  # FOUND-1,2 API-1,2,3 CHORE-1

    exported = client.get(f"{B}/export")
    assert exported.status_code == 200
    body = exported.get_data(as_text=True)
    assert "- [x] FOUND-1 · Scaffold the repo" in body
    assert "### EPIC API — REST surface" in body


# --------------------------------------------------------------------------- #
# PORT-6: robust full-sized-backlog import (batched, structured, size-capped)
# --------------------------------------------------------------------------- #
def _big_spec(n=1500, epics=6):
    """Synthetic SPEC.md of ~n tasks across a few epics."""
    out = ["# Big Project", "", "## Backlog", ""]
    per = n // epics
    made = 0
    for e in range(epics):
        out += [f"### EPIC EP{e} — Epic number {e}", ""]
        for i in range(per):
            made += 1
            out.append(
                f"- [ ] EP{e}-{i} · Task {made} title here (BE, P1). Some description.")
            out.append(f"  _Proof: pytest -k t{made}_")
        out.append("")
    return "\n".join(out), made


def test_import_large_backlog_is_fast_and_idempotent(client, project):
    """~1,500 tasks import with correct counts inside a sane time budget, and a
    re-import is idempotent (0 created; all unchanged)."""
    import time

    B = "/api/v1/projects/demo"
    body, n = _big_spec()
    t0 = time.time()
    r = client.post(f"{B}/import", data=body,
                    headers={"Content-Type": "text/markdown"})
    elapsed = time.time() - t0
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    j = r.get_json()
    assert j["total"] == n
    assert j["created"] == n
    assert j["failed"] == []
    # Generous budget: batched, this is ~1s locally; a bare-500 timeout was ~15s+.
    assert elapsed < 15, f"import of {n} tasks took {elapsed:.1f}s"

    # Re-import: idempotent — nothing created, everything unchanged (no writes).
    r2 = client.post(f"{B}/import", data=body,
                     headers={"Content-Type": "text/markdown"})
    assert r2.status_code == 200
    j2 = r2.get_json()
    assert j2["created"] == 0
    assert j2["updated"] == 0
    assert j2["unchanged"] == n


def test_import_malformed_task_is_reported_not_500(client, project):
    """A single malformed task (empty title) is reported in ``failed`` (HTTP 207),
    the request is NOT a 500, and the other tasks still import."""
    B = "/api/v1/projects/demo"
    spec = (
        "# Demo\n\n## Backlog\n\n"
        "- [ ] GOOD-1 · A fine task (BE).\n"
        "- [ ]\n"                          # empty (no key, no title) -> failure
        "- [ ] GOOD-2 · Another fine task.\n"
    )
    r = client.post(f"{B}/import", data=spec,
                    headers={"Content-Type": "text/markdown"})
    assert r.status_code == 207, r.get_data(as_text=True)[:400]
    j = r.get_json()
    assert j["created"] == 2
    assert len(j["failed"]) == 1
    assert "title" in j["failed"][0]["error"]
    assert j["total"] == 3  # 2 created + 1 failed

    keys = {t["key"] for t in client.get(f"{B}/tasks").get_json()}
    assert {"GOOD-1", "GOOD-2"} <= keys


def test_import_oversize_body_is_413(client, project):
    """A body over MAX_CONTENT_LENGTH is rejected with a useful 413, not a 500."""
    B = "/api/v1/projects/demo"
    cfg = client.application.config
    original = cfg["MAX_CONTENT_LENGTH"]
    cfg["MAX_CONTENT_LENGTH"] = 1024  # 1 KiB for the test
    try:
        big = "# Demo\n\n## Backlog\n\n" + ("- [ ] T · x\n" * 500)  # > 1 KiB
        assert len(big.encode()) > 1024
        r = client.post(f"{B}/import", data=big,
                        headers={"Content-Type": "text/markdown"})
        assert r.status_code == 413, r.get_data(as_text=True)[:400]
        assert "too large" in r.get_json()["message"].lower()
    finally:
        cfg["MAX_CONTENT_LENGTH"] = original


def test_export_diff_detects_change(client, project):
    B = "/api/v1/projects/demo"
    client.post(f"{B}/import", data=SAMPLE,
               headers={"Content-Type": "text/markdown"})
    # flip API-2 to done in a posted variant
    changed = SAMPLE.replace("- [ ] API-2", "- [x] API-2")
    diff = client.post(f"{B}/export/diff", data=changed,
                      headers={"Content-Type": "text/markdown"}).get_json()
    assert "API-2" in diff["message"]
    assert "1 changed" in diff["message"]
