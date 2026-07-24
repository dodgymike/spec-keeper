"""Integration tests for the RELIN relation-mirror backfill (SLS-J2 follow-up).

The backfill operates directly on the DynamoDB single table, so these run on the
DynamoDB backend only (they self-skip on Postgres). They prove:

* a pre-J2 forward-only relation (no mirror) gets its RELIN mirror created by
  ``--apply``, byte-identical to what ``add_relation`` writes, and then shows up
  as an INCOMING edge in ``list_relations`` on the destination task;
* re-running ``--apply`` is idempotent (no duplicate, skipped-existing counts it);
* ``--dry-run`` writes nothing (the mirror stays absent);
* a relation that already has its mirror (created via normal ``add_relation``) is
  left untouched.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.storage import keys as K

# Load the script as a module by file path (it lives in scripts/, not a package).
_SPEC = importlib.util.spec_from_file_location(
    "backfill_relation_mirrors",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "backfill_relation_mirrors.py",
)
brm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(brm)


@pytest.fixture
def dynamo_app(app):
    """The app, guarded to the DynamoDB backend (the backfill targets the table)."""
    if app._backend != "dynamodb":
        pytest.skip("relation-mirror backfill operates on the DynamoDB table.")
    return app


def _make_task(client, slug, key, title):
    r = client.post(f"/api/v1/projects/{slug}/tasks", json={"key": key, "title": title})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["public_id"]


def _add_relation(client, slug, src_key, dst_key, kind):
    r = client.post(f"/api/v1/projects/{slug}/tasks/{src_key}/relations",
                    json={"target": dst_key, "kind": kind})
    assert r.status_code == 201, r.get_json()


def _incoming(client, slug, ident):
    r = client.get(f"/api/v1/projects/{slug}/tasks/{ident}/relations")
    assert r.status_code == 200, r.get_json()
    return [e for e in r.get_json() if e["direction"] == "incoming"]


def _get(table, pk, sk):
    return table.get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _seed_forward_only(dynamo_app, client, *, src_key, dst_key, kind):
    """Create a real relation, then delete its RELIN mirror to reproduce a
    pre-J2 forward-only relation. Returns (table, pk, mirror_sk, original_mirror,
    src_pubid)."""
    slug = "demo"
    src = _make_task(client, slug, src_key, "Source")
    dst = _make_task(client, slug, dst_key, "Dest")
    _add_relation(client, slug, src_key, dst_key, kind)

    table = dynamo_app._dynamo_table
    pk = K.pk(slug)
    mirror_sk = K.relation_in_sk(dst, kind, src)
    original_mirror = _get(table, pk, mirror_sk)
    assert original_mirror is not None, "add_relation should have written a mirror"
    # Simulate a pre-J2 relation: drop the mirror, keep the forward item.
    table.delete_item(Key={"PK": pk, "SK": mirror_sk})
    assert _get(table, pk, mirror_sk) is None
    return table, pk, mirror_sk, original_mirror, src


def test_apply_creates_missing_mirror_and_is_idempotent(dynamo_app, client, project):
    table, pk, mirror_sk, original_mirror, src = _seed_forward_only(
        dynamo_app, client, src_key="A-1", dst_key="A-2", kind="blocks")

    # Pre-J2 bug reproduced: the incoming edge is missing.
    assert _incoming(client, "demo", "A-2") == []

    # --apply creates exactly the one missing mirror.
    summary = brm.backfill(table, apply=True, log=lambda *a: None)
    assert (summary.scanned, summary.created, summary.skipped_existing) == (1, 1, 0)

    # The recreated mirror is byte-identical to what add_relation wrote.
    assert _get(table, pk, mirror_sk) == original_mirror

    # list_relations on the destination now returns the incoming edge.
    inc = _incoming(client, "demo", "A-2")
    assert len(inc) == 1
    assert inc[0]["kind"] == "blocks"
    assert inc[0]["task"] == "A-1"

    # Re-run: idempotent — no duplicate, counted as skipped-existing.
    again = brm.backfill(table, apply=True, log=lambda *a: None)
    assert (again.scanned, again.created, again.skipped_existing) == (1, 0, 1)
    assert _get(table, pk, mirror_sk) == original_mirror


def test_dry_run_writes_nothing(dynamo_app, client, project):
    table, pk, mirror_sk, _original, _src = _seed_forward_only(
        dynamo_app, client, src_key="B-1", dst_key="B-2", kind="relates")

    summary = brm.backfill(table, apply=False, log=lambda *a: None)
    assert (summary.scanned, summary.created, summary.skipped_existing) == (1, 1, 0)

    # Dry-run must not have written the mirror.
    assert _get(table, pk, mirror_sk) is None
    assert _incoming(client, "demo", "B-2") == []


def test_existing_mirror_is_left_untouched(dynamo_app, client, project):
    slug = "demo"
    _make_task(client, slug, "C-1", "Source")
    _make_task(client, slug, "C-2", "Dest")
    _add_relation(client, slug, "C-1", "C-2", "follow_up")  # writes forward + mirror

    table = dynamo_app._dynamo_table
    dst = client.get(f"/api/v1/projects/{slug}/tasks/C-2").get_json()["public_id"]
    src = client.get(f"/api/v1/projects/{slug}/tasks/C-1").get_json()["public_id"]
    mirror_sk = K.relation_in_sk(dst, "follow_up", src)
    before = _get(table, K.pk(slug), mirror_sk)
    assert before is not None

    summary = brm.backfill(table, apply=True, log=lambda *a: None)
    assert (summary.scanned, summary.created, summary.skipped_existing) == (1, 0, 1)
    assert _get(table, K.pk(slug), mirror_sk) == before  # unchanged
