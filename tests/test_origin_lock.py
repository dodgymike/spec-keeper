"""SEC-EDGE-1: origin-lock the API with a staged off/warn/enforce switch.

The raw API Gateway execute-api hostname bypasses Cloudflare's WAF/rate limits.
Cloudflare injects a shared-secret header (``ORIGIN_LOCK_HEADER``) on traffic it
proxies; when enforcing, the app must reject any request lacking it. The rollout
is staged (off -> warn -> enforce) so live agents are never broken before we
confirm Cloudflare actually injects the header:

* ``off`` (or an empty secret) -> no-op: current behaviour, requests pass.
* ``warn``    -> log a WARNING on a bad header but do NOT block.
* ``enforce`` -> 403 on a missing/invalid header, pass on a correct one.

These build their own apps (like ``test_boot_guard``/``test_auth``) so they are
not dragged into the storage-backend parametrisation; a lightweight probe route
that needs no DB is registered so we can exercise the gate in isolation.
"""
from __future__ import annotations

import logging

import pytest

from app import create_app
from app.config import TestConfig

SECRET = "cf-shared-secret-value"
HEADER = "X-Origin-Lock"


def _make_client(mode, secret=SECRET, header=HEADER):
    class Cfg(TestConfig):
        ORIGIN_LOCK_MODE = mode
        ORIGIN_LOCK_SECRET = secret
        ORIGIN_LOCK_HEADER = header

    app = create_app(Cfg)

    @app.route("/_probe")
    def _probe():
        return {"ok": True}

    return app, app.test_client()


def test_off_lets_request_through_without_header():
    _, client = _make_client("off")
    resp = client.get("/_probe")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_empty_secret_enforce_is_treated_as_off():
    # Fail-open safety net: enforce with no secret must NOT block.
    _, client = _make_client("enforce", secret="")
    resp = client.get("/_probe")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_enforce_correct_header_passes():
    _, client = _make_client("enforce")
    resp = client.get("/_probe", headers={HEADER: SECRET})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_enforce_missing_header_is_403():
    _, client = _make_client("enforce")
    resp = client.get("/_probe")
    assert resp.status_code == 403
    # Generic message: no hint that an origin header is expected.
    assert "origin" not in (resp.get_data(as_text=True).lower())


def test_enforce_wrong_header_is_403():
    _, client = _make_client("enforce")
    resp = client.get("/_probe", headers={HEADER: "not-the-secret"})
    assert resp.status_code == 403


def test_warn_wrong_header_passes_and_logs(caplog):
    app, client = _make_client("warn")
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        resp = client.get("/_probe", headers={HEADER: "not-the-secret"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("origin-lock: request without valid origin header" in m for m in msgs)
    # The secret and the provided value must NEVER be logged.
    joined = " ".join(msgs)
    assert SECRET not in joined
    assert "not-the-secret" not in joined


def test_warn_correct_header_passes_without_logging(caplog):
    app, client = _make_client("warn")
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        resp = client.get("/_probe", headers={HEADER: SECRET})
    assert resp.status_code == 200
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("origin-lock" in m for m in msgs)


def test_enforce_empty_secret_startup_warns(caplog):
    # Misconfig heads-up at startup (does not crash).
    class Cfg(TestConfig):
        ORIGIN_LOCK_MODE = "enforce"
        ORIGIN_LOCK_SECRET = ""

    with caplog.at_level(logging.WARNING):
        app = create_app(Cfg)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("ORIGIN_LOCK_SECRET is empty" in m for m in msgs)
    assert app is not None


def test_enforce_non_ascii_header_is_403_not_500():
    # A hostile non-ASCII header value must be a clean 403 mismatch, never a 500
    # (hmac.compare_digest on str raises TypeError for non-ASCII — we compare bytes).
    _, client = _make_client("enforce")
    resp = client.get("/_probe", headers={HEADER: "ÿþ"})
    assert resp.status_code == 403


def test_custom_header_name_is_honoured():
    _, client = _make_client("enforce", header="X-CF-Secret")
    # Default header name carries the secret but the configured one does not.
    resp = client.get("/_probe", headers={"X-Origin-Lock": SECRET})
    assert resp.status_code == 403
    resp = client.get("/_probe", headers={"X-CF-Secret": SECRET})
    assert resp.status_code == 200
