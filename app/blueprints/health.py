"""Liveness/readiness probes."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """Liveness: process is up (no backend dependency)."""
    return jsonify(status="ok")


@bp.get("/readyz")
def readyz():
    """Readiness: the configured storage backend answers a cheap liveness probe.

    Backend-aware (SLS-14): delegates to ``current_app.storage.ping()`` so it works
    whatever ``STORAGE_BACKEND`` is (Postgres ``SELECT 1`` / DynamoDB DescribeTable),
    instead of hard-pinging Postgres. Response shapes are unchanged."""
    try:
        current_app.storage.ping()
        return jsonify(status="ready")
    except Exception as exc:
        # SEC-FIX-6: /readyz is UNAUTHENTICATED, so the raw backend exception text
        # (which can name the DB host/dbname, SQL, or DynamoDB table) must not be
        # echoed to the client. Return a coarse status + generic reason and keep the
        # real detail server-side only.
        current_app.logger.error("readyz: backend ping failed: %s", exc)
        return jsonify(status="unready", reason="backend ping failed"), 503
