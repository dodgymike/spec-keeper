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

    # idempotent: second import creates no new tasks
    imp2 = client.post(f"{B}/import", data=SAMPLE,
                       headers={"Content-Type": "text/markdown"})
    assert "0 task(s) created" in imp2.get_json()["message"]

    tasks = client.get(f"{B}/tasks").get_json()
    assert len(tasks) == 6  # FOUND-1,2 API-1,2,3 CHORE-1

    exported = client.get(f"{B}/export")
    assert exported.status_code == 200
    body = exported.get_data(as_text=True)
    assert "- [x] FOUND-1 · Scaffold the repo" in body
    assert "### EPIC API — REST surface" in body


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
