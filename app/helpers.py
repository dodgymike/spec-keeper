"""Shared request helpers: lookups, optimistic-lock checks, auth."""
from __future__ import annotations

import sqlalchemy as sa
from flask import current_app, request
from flask_smorest import abort

from .extensions import db
from .models import Epic, Project, Task


def require_api_key() -> None:
    """If API_KEYS is configured, enforce a bearer token. No-op when empty
    (the default for a local-only deployment)."""
    keys = current_app.config.get("API_KEYS") or []
    if not keys:
        return
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if token not in keys:
        abort(401, message="Missing or invalid bearer token.")


def get_project_or_404(slug: str) -> Project:
    project = db.session.execute(
        sa.select(Project).where(Project.slug == slug)
    ).scalar_one_or_none()
    if project is None:
        abort(404, message=f"Project '{slug}' not found.")
    return project


def get_epic_or_404(project_id: int, key: str) -> Epic:
    epic = db.session.execute(
        sa.select(Epic).where(Epic.project_id == project_id, Epic.key == key)
    ).scalar_one_or_none()
    if epic is None:
        abort(404, message=f"Epic '{key}' not found.")
    return epic


def get_task_or_404(project_id: int, ident: str) -> Task:
    """Look up a task by human key first, then public_id."""
    task = db.session.execute(
        sa.select(Task).where(Task.project_id == project_id, Task.key == ident)
    ).scalar_one_or_none()
    if task is None:
        task = db.session.execute(
            sa.select(Task).where(
                Task.project_id == project_id, Task.public_id == ident
            )
        ).scalar_one_or_none()
    if task is None:
        abort(404, message=f"Task '{ident}' not found.")
    return task


def check_if_match(task: Task) -> None:
    """Enforce optimistic locking. If the client sent If-Match it must equal
    the current task version, otherwise 412. If absent, the write proceeds
    (lenient for non-concurrent callers), matching simple agent usage."""
    if_match = request.headers.get("If-Match")
    if if_match is None:
        return
    expected = if_match.strip().strip('"').lstrip("v")
    if str(task.version) != expected:
        abort(
            412,
            message=(
                f"Version conflict: task is at v{task.version}, "
                f"you sent If-Match {if_match!r}. Re-read and retry."
            ),
        )


def etag_headers(task) -> dict:
    """Build the ETag header from anything carrying a ``version`` (ORM or DTO)."""
    return {"ETag": f'"v{task.version}"'}


def expected_version_from_request() -> str | None:
    """Parse the ``If-Match`` request header into a bare version token.

    Returns the value with surrounding quotes and a leading ``v`` stripped
    (e.g. ``'"v3"'`` -> ``'3'``), or ``None`` when the header is absent. The
    storage layer compares this against the task's current version and raises
    ``VersionConflict`` (-> 412) on mismatch — preserving the old lenient
    behaviour where a missing header skips the check.
    """
    if_match = request.headers.get("If-Match")
    if if_match is None:
        return None
    return if_match.strip().strip('"').lstrip("v")
