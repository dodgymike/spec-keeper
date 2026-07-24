"""SEC-FIX-5: the public per-IP rate limiter must only trust the Cloudflare
forwarding header (``CF-Connecting-IP``) when origin-lock is EFFECTIVELY enforcing.

On the raw ``execute-api`` host (origin-lock off/warn, or enforce-without-secret)
``CF-Connecting-IP`` / ``X-Forwarded-For`` are attacker-forgeable, so the limiter
must key on the real peer (``remote_addr``); an attacker rotating a spoofed header
per request must therefore share ONE bucket. Only when origin-lock is enforcing
(mode == "enforce" AND a secret is set) is ``CF-Connecting-IP`` a trusted key.

The tests drive the real signup route with a key-capturing in-memory limiter, so
they exercise the SAME ``client_ip()`` helper both signup and enroll use.
"""
from __future__ import annotations

import pytest

from app import create_app, signup_ratelimit
from app.blueprints import signup as signup_bp
from app.client_ip import client_ip
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL

_ENFORCE_SECRET = "origin-lock-secret"
_ENFORCE_HEADER = "X-Origin-Lock"


class KeyCaptureTable:
    """Fake DynamoDB limiter table: records every counter pk and returns a
    per-bucket monotonically increasing count (so MAX=1 trips the 2nd hit)."""

    def __init__(self):
        self.counts: dict = {}
        self.keys: list = []

    def update_item(self, Key, **kw):
        pk = Key["pk"]
        self.keys.append(pk)
        self.counts[pk] = self.counts.get(pk, 0) + 1
        return {"Attributes": {"count": self.counts[pk]}}


@pytest.fixture
def rl_table(monkeypatch):
    table = KeyCaptureTable()
    signup_ratelimit.set_table_factory(lambda cfg: table)
    # Intake enqueues on the happy path; stub SQS so no real AWS call is made.
    monkeypatch.setattr(signup_bp.signup_aws, "send_intake_message", lambda cfg, msg: True)
    yield table
    signup_ratelimit.set_table_factory(None)


def _app(**overrides):
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        SIGNUPS_TABLE = "spec-server-signups"
        SIGNUP_INTAKE_QUEUE_URL = "https://sqs.local/intake"
        SIGNUP_RATELIMIT_TABLE = "rl"
        SIGNUP_RATELIMIT_MAX = 1  # 2nd hit in the SAME bucket -> 429
    for k, v in overrides.items():
        setattr(_Cfg, k, v)
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


def _bucket_ips(table):
    """The IP portion of each captured counter pk (drops the ``prefix`` and the
    trailing ``#<window>``)."""
    ips = []
    for pk in table.keys:
        assert pk.startswith("sig#ip#")
        ips.append(pk[len("sig#ip#"):].rsplit("#", 1)[0])
    return ips


# --------------------------------------------------------------------------- #
# NOT enforcing -> forwarding headers ignored; limiter keys on remote_addr
# --------------------------------------------------------------------------- #
def test_off_mode_ignores_spoofed_headers_shared_bucket(rl_table):
    """Origin-lock off: two requests from the SAME remote_addr but DIFFERENT
    spoofed CF-Connecting-IP values share ONE bucket, so the 2nd trips the floor."""
    c = _app(ORIGIN_LOCK_MODE="off").test_client()
    r1 = c.post("/api/v1/signup", json={"email": "a@example.com"},
                headers={"CF-Connecting-IP": "1.1.1.1"},
                environ_base={"REMOTE_ADDR": "10.0.0.9"})
    r2 = c.post("/api/v1/signup", json={"email": "b@example.com"},
                headers={"CF-Connecting-IP": "2.2.2.2"},
                environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert (r1.status_code, r2.status_code) == (202, 429)
    assert _bucket_ips(rl_table) == ["10.0.0.9", "10.0.0.9"]  # remote_addr, not the header


def test_off_mode_ignores_spoofed_xff(rl_table):
    """A rotated X-Forwarded-For is likewise ignored off-mode (never a key source)."""
    c = _app(ORIGIN_LOCK_MODE="off").test_client()
    r1 = c.post("/api/v1/signup", json={"email": "a@example.com"},
                headers={"X-Forwarded-For": "9.9.9.9, 3.3.3.3"},
                environ_base={"REMOTE_ADDR": "10.0.0.7"})
    r2 = c.post("/api/v1/signup", json={"email": "b@example.com"},
                headers={"X-Forwarded-For": "8.8.8.8"},
                environ_base={"REMOTE_ADDR": "10.0.0.7"})
    assert (r1.status_code, r2.status_code) == (202, 429)
    assert _bucket_ips(rl_table) == ["10.0.0.7", "10.0.0.7"]


def test_enforce_without_secret_ignores_headers(rl_table):
    """enforce mode but NO secret degrades to off (the gate is disabled), so the
    forwarding header is still untrusted -> keys on remote_addr."""
    c = _app(ORIGIN_LOCK_MODE="enforce", ORIGIN_LOCK_SECRET="").test_client()
    r1 = c.post("/api/v1/signup", json={"email": "a@example.com"},
                headers={"CF-Connecting-IP": "1.1.1.1"},
                environ_base={"REMOTE_ADDR": "10.0.0.5"})
    r2 = c.post("/api/v1/signup", json={"email": "b@example.com"},
                headers={"CF-Connecting-IP": "2.2.2.2"},
                environ_base={"REMOTE_ADDR": "10.0.0.5"})
    assert (r1.status_code, r2.status_code) == (202, 429)
    assert _bucket_ips(rl_table) == ["10.0.0.5", "10.0.0.5"]


# --------------------------------------------------------------------------- #
# Enforcing + secret -> CF-Connecting-IP is the trusted key
# --------------------------------------------------------------------------- #
def test_enforce_mode_uses_cf_connecting_ip(rl_table):
    """enforce + secret: CF-Connecting-IP IS the key. Two requests with DIFFERENT
    CF IPs land in DIFFERENT buckets (both under the floor); a repeat of the first
    CF IP trips it -> proves the header (not remote_addr) is the key."""
    c = _app(ORIGIN_LOCK_MODE="enforce", ORIGIN_LOCK_SECRET=_ENFORCE_SECRET,
             ORIGIN_LOCK_HEADER=_ENFORCE_HEADER).test_client()
    common = {_ENFORCE_HEADER: _ENFORCE_SECRET}  # pass the origin-lock gate

    r1 = c.post("/api/v1/signup", json={"email": "a@example.com"},
                headers={**common, "CF-Connecting-IP": "1.1.1.1"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"})
    r2 = c.post("/api/v1/signup", json={"email": "b@example.com"},
                headers={**common, "CF-Connecting-IP": "2.2.2.2"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"})
    r3 = c.post("/api/v1/signup", json={"email": "c@example.com"},
                headers={**common, "CF-Connecting-IP": "1.1.1.1"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"})
    assert (r1.status_code, r2.status_code, r3.status_code) == (202, 202, 429)
    assert _bucket_ips(rl_table) == ["1.1.1.1", "2.2.2.2", "1.1.1.1"]


def test_enforce_mode_neutral_202_body_unchanged(rl_table):
    """The uniform-202 anti-enumeration body is unchanged under enforce mode."""
    from app import signup as signup_lib
    c = _app(ORIGIN_LOCK_MODE="enforce", ORIGIN_LOCK_SECRET=_ENFORCE_SECRET,
             ORIGIN_LOCK_HEADER=_ENFORCE_HEADER).test_client()
    r = c.post("/api/v1/signup", json={"email": "new@example.com"},
               headers={_ENFORCE_HEADER: _ENFORCE_SECRET, "CF-Connecting-IP": "5.5.5.5"})
    assert r.status_code == 202
    assert r.get_json() == signup_lib.uniform_intake_body()


# --------------------------------------------------------------------------- #
# Direct helper unit tests
# --------------------------------------------------------------------------- #
def test_client_ip_helper_off_mode_uses_remote_addr():
    app = _app(ORIGIN_LOCK_MODE="off")
    with app.test_request_context(
        "/", headers={"CF-Connecting-IP": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "10.9.9.9"}
    ):
        assert client_ip() == "10.9.9.9"


def test_client_ip_helper_enforce_uses_cf_header():
    app = _app(ORIGIN_LOCK_MODE="enforce", ORIGIN_LOCK_SECRET=_ENFORCE_SECRET)
    with app.test_request_context(
        "/", headers={"CF-Connecting-IP": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "10.9.9.9"}
    ):
        assert client_ip() == "1.2.3.4"


def test_client_ip_helper_enforce_falls_back_when_cf_absent():
    app = _app(ORIGIN_LOCK_MODE="enforce", ORIGIN_LOCK_SECRET=_ENFORCE_SECRET)
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.9.9.9"}):
        assert client_ip() == "10.9.9.9"
