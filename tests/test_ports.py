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


# --------------------------------------------------------------------------- #
# PORT-8: lossless full-fidelity JSON export/import (keyless tasks survive)
# --------------------------------------------------------------------------- #
def _seed_mixed_backlog(client, base):
    """A backlog with an epic, KEYED tasks (+tags, +epic) AND KEYLESS tasks —
    the mix that the SPEC.md transport cannot represent losslessly."""
    assert client.post(f"{base}/epics",
                        json={"key": "MIG", "title": "Migration",
                              "section": "backlog"}).status_code == 201
    # keyed, in an epic, with tags
    assert client.post(f"{base}/tasks", json={
        "key": "MIG-1", "epic_key": "MIG", "title": "Keyed one",
        "priority": "P1", "component": "BE", "tags": ["urgent", "backend"],
        "position": 1.0}).status_code == 201
    # keyless (no key) — the tasks the SPEC.md format silently drops
    for i in range(3):
        r = client.post(f"{base}/tasks", json={
            "title": f"Keyless follow-up {i}", "tags": ["followup"],
            "position": 10.0 + i})
        assert r.status_code == 201
        assert r.get_json()["key"] is None
    # a keyed task with no epic
    assert client.post(f"{base}/tasks", json={
        "key": "MIG-2", "title": "Keyed two", "position": 2.0}).status_code == 201


def test_json_export_includes_keyless_tasks(client, project):
    B = "/api/v1/projects/demo"
    _seed_mixed_backlog(client, B)

    resp = client.get(f"{B}/export?format=json")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    doc = resp.get_json()
    assert doc["format"] == "spec-server-full/v1"
    assert doc["project"]["slug"] == "demo"
    assert {e["key"] for e in doc["epics"]} == {"MIG"}
    assert len(doc["tasks"]) == 5  # 2 keyed + 3 keyless — none dropped

    keyless = [t for t in doc["tasks"] if t["key"] is None]
    assert len(keyless) == 3
    assert all(t["public_id"] for t in keyless)  # every task carries its anchor
    assert all(t["tags"] == ["followup"] for t in keyless)

    # Accept-header negotiation is the other way to ask for JSON.
    r2 = client.get(f"{B}/export", headers={"Accept": "application/json"})
    assert r2.mimetype == "application/json"
    assert len(r2.get_json()["tasks"]) == 5
    # The default (no format / */*) is still the SPEC.md text mirror, unchanged.
    r3 = client.get(f"{B}/export")
    assert r3.mimetype == "text/markdown"


def test_json_roundtrip_into_fresh_project_is_lossless(client, project):
    """import(export(project)) into a FRESH project reproduces EVERY task — keyed
    AND keyless — with fields, tags, epic and preserved public_id; re-import is a
    genuine no-op; a changed field re-imports as exactly one update."""
    src = "/api/v1/projects/demo"
    _seed_mixed_backlog(client, src)
    doc = client.get(f"{src}/export?format=json").get_json()
    src_by_pub = {t["public_id"]: t for t in doc["tasks"]}

    # Simulate a real migration to a fresh SERVER: the source is gone (in one DB,
    # task public_id is globally unique on Postgres, so preservation requires the
    # source not to coexist — Dynamo scopes it per project and behaves the same).
    assert client.delete(src).status_code in (200, 204)

    # Fresh, empty destination project.
    assert client.post("/api/v1/projects",
                       json={"slug": "dest", "name": "Dest"}).status_code == 201
    dst = "/api/v1/projects/dest"

    imp = client.post(f"{dst}/import", json=doc)
    assert imp.status_code == 200, imp.get_data(as_text=True)[:400]
    body = imp.get_json()
    assert body["created"] == 5
    assert body["updated"] == 0 and body["unchanged"] == 0
    assert body["failed"] == []
    assert body["epics_created"] == 1

    # Every task present with correct fields + preserved public_id (keyed+keyless).
    got = client.get(f"{dst}/tasks?limit=1000").get_json()
    assert len(got) == 5
    got_by_pub = {t["public_id"]: t for t in got}
    assert set(got_by_pub) == set(src_by_pub)  # public_ids preserved exactly

    # The core bug: the KEYLESS tasks survived the round-trip.
    keyless = [t for t in got if t["key"] is None]
    assert len(keyless) == 3
    assert all(set(t["tags"]) == {"followup"} for t in keyless)

    # A keyed task kept its epic + tags + priority + component.
    mig1 = next(t for t in got if t["key"] == "MIG-1")
    assert mig1["epic_key"] == "MIG"
    assert set(mig1["tags"]) == {"urgent", "backend"}
    assert mig1["priority"] == "P1" and mig1["component"] == "BE"

    # Re-import the SAME document into the SAME project -> genuine no-op.
    reimp = client.post(f"{dst}/import", json=doc).get_json()
    assert reimp["created"] == 0
    assert reimp["updated"] == 0
    assert reimp["unchanged"] == 5

    # Change ONE task (a keyless one) and re-import -> exactly one update.
    target_pub = keyless[0]["public_id"]
    changed = {**doc, "tasks": [
        {**t, "title": "Edited keyless title"} if t["public_id"] == target_pub else t
        for t in doc["tasks"]
    ]}
    upd = client.post(f"{dst}/import", json=changed).get_json()
    assert upd["updated"] == 1
    assert upd["unchanged"] == 4
    assert upd["created"] == 0
    edited = client.get(f"{dst}/tasks/{target_pub}").get_json()
    assert edited["title"] == "Edited keyless title"


def test_json_import_rejects_non_object_body(client, project):
    B = "/api/v1/projects/demo"
    r = client.post(f"{B}/import", json=[1, 2, 3])
    assert r.status_code == 400
    assert "JSON object" in r.get_json()["message"]


def test_json_import_reports_bad_task_not_500(client, project):
    """A task with an empty title is reported in ``failed`` (207); the rest import.
    Keyless-and-anchored tasks import fine alongside it."""
    B = "/api/v1/projects/demo"
    doc = {
        "format": "spec-server-full/v1",
        "project": {"slug": "demo", "name": "Demo"},
        "epics": [],
        "tasks": [
            {"public_id": "11111111-1111-1111-1111-111111111111",
             "key": "OK-1", "title": "Fine", "status": "todo",
             "section": "backlog", "position": 1.0, "tags": []},
            {"public_id": "22222222-2222-2222-2222-222222222222",
             "key": None, "title": "   ", "status": "todo",
             "section": "backlog", "position": 2.0, "tags": []},  # empty title
        ],
    }
    r = client.post(f"{B}/import", json=doc)
    assert r.status_code == 207, r.get_data(as_text=True)[:400]
    j = r.get_json()
    assert j["created"] == 1
    assert len(j["failed"]) == 1
    assert "title" in j["failed"][0]["error"]


def test_json_import_dedupes_repeated_public_id(client, project):
    """Two payload rows sharing one public_id collapse to a single upsert (last
    wins) — identical on both backends (no IntegrityError on Postgres, no silent
    divergence on DynamoDB)."""
    B = "/api/v1/projects/demo"
    pub = "33333333-3333-3333-3333-333333333333"
    doc = {
        "format": "spec-server-full/v1",
        "project": {"slug": "demo", "name": "Demo"},
        "epics": [],
        "tasks": [
            {"public_id": pub, "key": None, "title": "First write",
             "status": "todo", "section": "backlog", "position": 1.0, "tags": []},
            {"public_id": pub, "key": None, "title": "Last write wins",
             "status": "todo", "section": "backlog", "position": 1.0, "tags": []},
        ],
    }
    r = client.post(f"{B}/import", json=doc)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    assert r.get_json()["created"] == 1
    got = client.get(f"{B}/tasks").get_json()
    assert len(got) == 1
    assert got[0]["title"] == "Last write wins"


def _doc_with_task(**task_over):
    task = {
        "public_id": "44444444-4444-4444-4444-444444444444",
        "key": "BAD-1", "title": "Crafted", "status": "todo",
        "section": "backlog", "position": 1.0, "tags": [],
    }
    task.update(task_over)
    return {
        "format": "spec-server-full/v1",
        "project": {"slug": "demo", "name": "Demo"},
        "epics": [],
        "tasks": [task],
    }


def test_json_import_rejects_bad_status_enum(client, project):
    """SEC-FIX-7: the full-fidelity import schema now gates ``status`` with OneOf,
    so an out-of-range enum is a clean 422 (whole doc rejected) and NOT persisted."""
    B = "/api/v1/projects/demo"
    r = client.post(f"{B}/import", json=_doc_with_task(status="pwned"))
    assert r.status_code == 422, r.get_data(as_text=True)[:400]
    assert client.get(f"{B}/tasks").get_json() == []


def test_json_import_rejects_bad_priority_enum(client, project):
    """SEC-FIX-7: ``priority`` is gated with OneOf like TaskIn."""
    B = "/api/v1/projects/demo"
    r = client.post(f"{B}/import", json=_doc_with_task(priority="P99"))
    assert r.status_code == 422, r.get_data(as_text=True)[:400]
    assert client.get(f"{B}/tasks").get_json() == []


def test_json_import_rejects_bad_section_enum(client, project):
    """SEC-FIX-7: ``section`` is now gated (it was validated nowhere before)."""
    B = "/api/v1/projects/demo"
    r = client.post(f"{B}/import", json=_doc_with_task(section="nonsense"))
    assert r.status_code == 422, r.get_data(as_text=True)[:400]
    assert client.get(f"{B}/tasks").get_json() == []


def test_json_import_valid_enums_still_succeed(client, project):
    """A valid full-fidelity doc (in-range status/priority/section) still imports."""
    B = "/api/v1/projects/demo"
    r = client.post(f"{B}/import", json=_doc_with_task(
        status="in_progress", priority="P1", section="to_do"))
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    got = client.get(f"{B}/tasks").get_json()
    assert len(got) == 1
    assert got[0]["section"] == "to_do"


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
