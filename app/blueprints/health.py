"""Liveness/readiness probes."""
from __future__ import annotations

import sqlalchemy as sa
from flask import Blueprint, jsonify

from ..extensions import db

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """Liveness: process is up (no DB dependency)."""
    return jsonify(status="ok")


@bp.get("/readyz")
def readyz():
    """Readiness: the database answers."""
    try:
        db.session.execute(sa.text("SELECT 1"))
        return jsonify(status="ready")
    except Exception as exc:  # pragma: no cover - exercised via integration
        return jsonify(status="unready", error=str(exc)), 503
