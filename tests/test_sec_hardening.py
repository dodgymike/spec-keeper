"""SEC-FIX-2/3/4/6: app-level security hardening.

Covers four related hardening tasks that share app/config.py + app/__init__.py:
- SEC-FIX-4: AUTH_LEEWAY default is 45s (clock-skew tolerance).
- SEC-FIX-3: a loud boot WARNING when COGNITO_ISSUER is set but COGNITO_AUDIENCE
  is empty (audience/client_id check fail-open).
- SEC-FIX-2: a loud boot WARNING when COGNITO_ISSUER is set but the origin lock
  is not enforcing (raw execute-api bypass unmitigated).
- SEC-FIX-6: backend exception text never reaches the client (generic 503 handler
  + unauthenticated /readyz path is covered in test_health.py).
"""
from __future__ import annotations

import logging

from app import create_app
from app.config import Config, TestConfig
from app.storage.errors import BackendUnavailable

_ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_SECFIX"


# --- SEC-FIX-4: AUTH_LEEWAY default --------------------------------------- #
def test_auth_leeway_defaults_to_45():
    # The shipped default (no AUTH_LEEWAY env override) tolerates benign skew.
    assert Config.AUTH_LEEWAY == 45


# --- SEC-FIX-3 / SEC-FIX-2: boot warnings (non-bricking) ------------------ #
def _build(cfg_cls, caplog):
    with caplog.at_level(logging.WARNING):
        app = create_app(cfg_cls)
    return app


def test_warns_when_issuer_set_but_audience_empty(caplog):
    class Cfg(TestConfig):
        COGNITO_ISSUER = _ISSUER
        COGNITO_AUDIENCE = []
        # Keep the origin lock enforcing so we isolate the audience warning.
        ORIGIN_LOCK_MODE = "enforce"
        ORIGIN_LOCK_SECRET = "shh"

    app = _build(Cfg, caplog)
    assert app is not None  # still boots (warning, not a hard fail)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "COGNITO_AUDIENCE" in msgs
    assert "fail-open" in msgs


def test_warns_when_issuer_set_but_origin_lock_not_enforce(caplog):
    class Cfg(TestConfig):
        COGNITO_ISSUER = _ISSUER
        COGNITO_AUDIENCE = ["agents-client-id"]  # silence the audience warning
        ORIGIN_LOCK_MODE = "off"
        ORIGIN_LOCK_SECRET = ""

    app = _build(Cfg, caplog)
    assert app is not None  # still boots
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "origin-lock" in msgs
    assert "execute-api" in msgs


def test_warns_when_origin_lock_enforce_but_secret_empty(caplog):
    class Cfg(TestConfig):
        COGNITO_ISSUER = _ISSUER
        COGNITO_AUDIENCE = ["agents-client-id"]
        ORIGIN_LOCK_MODE = "enforce"
        ORIGIN_LOCK_SECRET = ""  # enforce with no secret => bypass not mitigated

    app = _build(Cfg, caplog)
    assert app is not None
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "origin-lock" in msgs


def test_no_hardening_warnings_when_prod_config_correct(caplog):
    class Cfg(TestConfig):
        COGNITO_ISSUER = _ISSUER
        COGNITO_AUDIENCE = ["agents-client-id"]
        ORIGIN_LOCK_MODE = "enforce"
        ORIGIN_LOCK_SECRET = "shh"

    app = _build(Cfg, caplog)
    assert app is not None
    joined = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "COGNITO_AUDIENCE" not in joined
    assert "origin-lock" not in joined


def test_no_warnings_when_issuer_unset_local_mode(caplog):
    # Local/auth-off mode (COGNITO_ISSUER unset) must not emit the prod warnings.
    app = _build(TestConfig, caplog)
    assert app is not None
    joined = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "COGNITO_AUDIENCE" not in joined
    assert "origin-lock" not in joined


# --- SEC-FIX-6: neutral generic 503 handler ------------------------------- #
_RAW = "psycopg OperationalError host=db.internal dbname=specserver password=hunter2"


def test_backend_unavailable_handler_is_neutral(client, monkeypatch):
    # GET /api/v1/projects -> storage.list_projects(); force it to raise with raw
    # backend text, then assert the client sees only the fixed neutral message.
    def boom():
        raise BackendUnavailable(_RAW)

    monkeypatch.setattr(client.application.storage, "list_projects", boom)
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["code"] == 503
    assert body["message"] == "Backend temporarily unavailable."
    raw_response = resp.get_data(as_text=True)
    assert _RAW not in raw_response
    assert "psycopg" not in raw_response
    assert "db.internal" not in raw_response
