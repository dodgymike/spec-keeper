"""Cross-backend parity test for storage.record_jira_sync (SLS-J4).

Runs against BOTH backends (Postgres + DynamoDB Local) via the parametrised
``app`` fixture. Proves the D2 contract: ``record_jira_sync`` writes ONLY the two
task attributes (``jira_issue_key`` / ``jira_sync_error``) identically on both
backends, and it does NOT perturb optimistic-locking or the UI delta feed —
``task.version`` is UNCHANGED and the ``/changes/head`` cursor is UNCHANGED
(no change-log entry was written). On error it emits the ``jira_sync_error``
audit event (the /events path — which is NOT the change-log delta feed).

Goes through ``current_app.storage`` directly (not ORM), so it is backend-neutral
and NOT ``postgres_only``.
"""
from __future__ import annotations

from flask import current_app


def _make_task(storage):
    storage.create_project({"slug": "jsync", "name": "JSync"})
    dto = storage.create_task("jsync", {"title": "Sync me", "description": "desc"})
    return dto.public_id


def test_record_jira_sync_sets_fields_no_version_no_cursor(app):
    """record_jira_sync sets both fields, leaves version + changes/head untouched."""
    with app.app_context():
        storage = current_app.storage
        pubid = _make_task(storage)

        before = storage.get_task("jsync", pubid)
        base_version = before.version
        base_head = storage.changes_head("jsync")
        assert before.jira_issue_key is None
        assert before.jira_sync_error is None

        # Record BOTH fields in one call (issue_key set + an error message).
        storage.record_jira_sync(
            "jsync", pubid, issue_key="PROJ-42", error="sync_task_created failed: boom"
        )

        after = storage.get_task("jsync", pubid)
        # (1) the two fields are set on BOTH backends
        assert after.jira_issue_key == "PROJ-42"
        assert after.jira_sync_error == "sync_task_created failed: boom"
        # (2) D2: task.version is UNCHANGED (no optimistic-lock bump)
        assert after.version == base_version
        # (3) D2: the /changes/head cursor is UNCHANGED (no change-log delta entry)
        assert storage.changes_head("jsync") == base_head

        # The jira_sync_error audit event WAS emitted (events != change-log feed).
        events = storage.list_events(
            "jsync", {"event_type": "jira_sync_error", "offset": 0, "limit": 50}
        )
        assert len(events) == 1
        assert "boom" in (events[0].message or "")


def test_record_jira_sync_success_clears_error_no_version_no_cursor(app):
    """A success record (error=None) clears jira_sync_error and still no bump/cursor."""
    with app.app_context():
        storage = current_app.storage
        pubid = _make_task(storage)

        # Seed an error first.
        storage.record_jira_sync("jsync", pubid, error="prior failure")
        mid = storage.get_task("jsync", pubid)
        assert mid.jira_sync_error == "prior failure"
        base_version = mid.version
        base_head = storage.changes_head("jsync")

        # Now a success: set issue_key, clear the error (error defaults to None).
        storage.record_jira_sync("jsync", pubid, issue_key="PROJ-7")

        after = storage.get_task("jsync", pubid)
        assert after.jira_issue_key == "PROJ-7"
        assert after.jira_sync_error is None
        assert after.version == base_version
        assert storage.changes_head("jsync") == base_head
