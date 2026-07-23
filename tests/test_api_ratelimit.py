"""SEC-DOS-2 — per-authenticated-principal (per Cognito ``sub``) API throttle.

Proves the additive, FAIL-OPEN limiter layered after JWT verification on the
``/api/v1`` data-plane:

* under the cap -> 200s;
* over the cap for a given ``sub`` -> 429 carrying ``Retry-After``;
* a DIFFERENT ``sub`` is unaffected by another sub's usage (per-tenant isolation);
* a counter/table error OR an unset table env => FAIL OPEN (allowed, no 429);
* public routes (no verified ``sub``) are never throttled;
* global ``spec-admins`` are exempt.

Like test_auth.py it mints RS256 tokens against an in-process keypair and serves
the matching JWKS by monkeypatching the fetch — no real Cognito. An in-memory
fake counter table is injected via ``api_ratelimit.set_table_factory``.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import api_ratelimit, create_app, helpers
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL

ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_DOSPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "test-agents-client-id"
KID = "dos-key-1"

GROUP_WRITE = "spec-writers"
GROUP_ADMIN = "spec-admins"

# Generous window so every request in a test lands in the SAME fixed window; a
# small cap so a handful of calls trips it.
WINDOW_S = 100
MAX = 3


# --------------------------------------------------------------------------- #
# Key material + JWKS (mirrors test_auth.py)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    algo = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    public_jwk = algo.to_jwk(rsa_key.public_key(), as_dict=True)
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, jwks):
    monkeypatch.setattr(helpers, "_http_get_json", lambda uri: jwks)
    helpers._reset_jwks_cache()
    yield
    helpers._reset_jwks_cache()


@pytest.fixture(autouse=True)
def _reset_factory():
    """Every test starts with the limiter fail-open (no table) unless it opts in."""
    api_ratelimit.set_table_factory(lambda cfg: None)
    yield
    api_ratelimit.set_table_factory(None)


def _mint(rsa_key, *, sub, groups=(GROUP_WRITE,)):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "client_id": AUDIENCE,
        "aud": AUDIENCE,
        "token_use": "access",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 3600,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _app():
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        COGNITO_ISSUER = ISSUER
        COGNITO_JWKS_URI = JWKS_URI
        COGNITO_AUDIENCE = [AUDIENCE]
        API_RATELIMIT_TABLE = "spec-server-signup-ratelimit"
        API_RATELIMIT_MAX = MAX
        API_RATELIMIT_WINDOW_S = WINDOW_S

    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


# --------------------------------------------------------------------------- #
# In-memory fake counter table (the fixed_window_incr shape only)
# --------------------------------------------------------------------------- #
class FakeCounter:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.calls = 0

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        self.calls += 1
        pk = Key["pk"]
        item = self.items.setdefault(pk, {"count": 0})
        item["count"] += ExpressionAttributeValues[":one"]
        item["ttl"] = ExpressionAttributeValues[":ttl"]
        return {"Attributes": {"count": item["count"]}}


class BoomCounter:
    def update_item(self, **kwargs):
        raise RuntimeError("dynamodb unavailable")


def _use(table):
    api_ratelimit.set_table_factory(lambda cfg: table)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_under_cap_all_ok(rsa_key):
    _use(FakeCounter())
    c = _app().test_client()
    tok = _mint(rsa_key, sub="sub-a")
    for _ in range(MAX):
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200


def test_over_cap_is_429_with_retry_after(rsa_key):
    _use(FakeCounter())
    c = _app().test_client()
    tok = _mint(rsa_key, sub="sub-a")
    for _ in range(MAX):
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200
    r = c.get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 429, r.get_json()
    assert r.headers.get("Retry-After") is not None
    assert int(r.headers["Retry-After"]) >= 1
    assert r.get_json()["code"] == 429


def test_a_different_sub_is_unaffected(rsa_key):
    """Per-tenant isolation: sub-b keeps working while sub-a is throttled."""
    _use(FakeCounter())
    c = _app().test_client()
    a = _mint(rsa_key, sub="sub-a")
    b = _mint(rsa_key, sub="sub-b")
    for _ in range(MAX + 2):
        c.get("/api/v1/projects", headers=_auth(a))
    assert c.get("/api/v1/projects", headers=_auth(a)).status_code == 429
    # sub-b's budget is untouched.
    for _ in range(MAX):
        assert c.get("/api/v1/projects", headers=_auth(b)).status_code == 200


def test_counter_error_fails_open(rsa_key):
    """A DynamoDB error must ALLOW the request (never 429 a legit caller)."""
    _use(BoomCounter())
    c = _app().test_client()
    tok = _mint(rsa_key, sub="sub-a")
    for _ in range(MAX + 5):
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200


def test_unset_table_fails_open(rsa_key):
    """No API_RATELIMIT_TABLE => limiter disabled => never throttles."""
    api_ratelimit.set_table_factory(lambda cfg: None)

    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        COGNITO_ISSUER = ISSUER
        COGNITO_JWKS_URI = JWKS_URI
        COGNITO_AUDIENCE = [AUDIENCE]
        API_RATELIMIT_TABLE = None
        API_RATELIMIT_MAX = MAX
        API_RATELIMIT_WINDOW_S = WINDOW_S

    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    c = app.test_client()
    tok = _mint(rsa_key, sub="sub-a")
    for _ in range(MAX + 5):
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200


def test_public_routes_not_throttled(rsa_key):
    """Public routes carry no verified sub -> never counted/throttled."""
    table = FakeCounter()
    _use(table)
    c = _app().test_client()
    for _ in range(MAX + 10):
        assert c.get("/readyz").status_code in (200, 503)
        assert c.get("/openapi.json").status_code == 200
    # The counter only ever sees data-plane (verified-sub) requests.
    assert table.calls == 0


def test_global_admin_is_exempt(rsa_key):
    """A spec-admins principal is exempt from the per-sub throttle."""
    table = FakeCounter()
    _use(table)
    c = _app().test_client()
    tok = _mint(rsa_key, sub="sub-admin", groups=[GROUP_ADMIN])
    for _ in range(MAX + 5):
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200
    assert table.calls == 0  # exempt principals never touch the counter


def test_check_rate_limit_unit_isolation():
    """Unit-level: distinct subs keep independent counts; retry_after >= 1."""
    table = FakeCounter()
    _use(table)
    cfg = {"API_RATELIMIT_TABLE": "t", "API_RATELIMIT_MAX": 2, "API_RATELIMIT_WINDOW_S": WINDOW_S}
    assert api_ratelimit.check_rate_limit(cfg, "x") == (False, 0)
    assert api_ratelimit.check_rate_limit(cfg, "x") == (False, 0)
    limited, retry = api_ratelimit.check_rate_limit(cfg, "x")
    assert limited and retry >= 1
    # A different sub is unaffected.
    assert api_ratelimit.check_rate_limit(cfg, "y") == (False, 0)


def test_check_rate_limit_empty_sub_fails_open():
    _use(FakeCounter())
    cfg = {"API_RATELIMIT_TABLE": "t", "API_RATELIMIT_MAX": 1, "API_RATELIMIT_WINDOW_S": WINDOW_S}
    assert api_ratelimit.check_rate_limit(cfg, "") == (False, 0)
    assert api_ratelimit.check_rate_limit(cfg, None) == (False, 0)
