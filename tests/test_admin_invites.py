"""Admin invite endpoint tests (HA-2): POST/GET /api/v1/admin/invites.

Two concerns:
  * Behaviour (auth OFF, the baseline suite's mode): minting stores only the
    code HASH active, the plaintext code is returned once, listing never leaks
    plaintext, and an unconfigured table yields 501.
  * Authz (Cognito ON): a non-admin token is 403; a spec-admins token is 201.
    Uses an in-process RSA/JWKS harness (mirrors test_auth) so no real Cognito
    is needed.

The invites table is faked in-memory (monkeypatched into
``app.blueprints.admin._invites_table``) so no DynamoDB Local is required.
"""
from __future__ import annotations

import hashlib
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app, helpers
from app.blueprints import admin as admin_bp
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


class FakeTable:
    """In-memory stand-in for the invites DynamoDB Table (resource API)."""

    def __init__(self):
        self.items: list[dict] = []

    def put_item(self, Item, ConditionExpression=None):
        # code_hash is unique in these tests; ignore the collision guard.
        self.items.append(dict(Item))
        return {}

    def scan(self, ExclusiveStartKey=None):
        return {"Items": list(self.items)}


@pytest.fixture
def fake_table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(admin_bp, "_invites_table", lambda cfg: t)
    return t


def _app(**overrides):
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
    for k, v in overrides.items():
        setattr(_Cfg, k, v)
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


# --------------------------------------------------------------------------- #
# Behaviour (auth off)
# --------------------------------------------------------------------------- #
def test_mint_stores_only_hash_active(fake_table):
    app = _app(INVITES_TABLE="spec-server-invites")
    r = app.test_client().post("/api/v1/admin/invites", json={})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    code = body["code"]
    assert code and body["code_hash"] == _sha(code)
    assert body["email_bound"] is False
    # The stored row carries only the HASH + status active — never the plaintext.
    assert len(fake_table.items) == 1
    stored = fake_table.items[0]
    assert stored["code_hash"] == _sha(code)
    assert stored["status"] == "active"
    assert code not in stored.values()
    assert "email_binding" not in stored


def test_mint_email_bound_stores_hash_not_plaintext(fake_table):
    app = _app(INVITES_TABLE="spec-server-invites")
    r = app.test_client().post("/api/v1/admin/invites", json={"email": "Bob@Example.com"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["email_bound"] is True
    stored = fake_table.items[0]
    # The address itself is never stored — only its (normalized) hash.
    assert stored["email_binding"] == _sha("bob@example.com")
    assert "bob@example.com" not in stored.values()


def test_list_never_leaks_plaintext(fake_table):
    app = _app(INVITES_TABLE="spec-server-invites")
    c = app.test_client()
    minted = c.post("/api/v1/admin/invites", json={}).get_json()
    r = c.get("/api/v1/admin/invites")
    assert r.status_code == 200
    rows = r.get_json()
    assert len(rows) == 1
    row = rows[0]
    assert row["code_hash"] == minted["code_hash"]
    assert row["status"] == "active"
    # No field carries the plaintext code.
    assert minted["code"] not in row.values()
    assert "code" not in row


def test_unconfigured_table_returns_501():
    # INVITES_TABLE unset (TestConfig default) -> graceful 501, no crash.
    app = _app()
    r = app.test_client().post("/api/v1/admin/invites", json={})
    assert r.status_code == 501
    r2 = app.test_client().get("/api/v1/admin/invites")
    assert r2.status_code == 501


# --------------------------------------------------------------------------- #
# Authz (Cognito on) — non-admin 403, admin 201
# --------------------------------------------------------------------------- #
ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_INVITEPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "invite-test-client"
KID = "invite-key-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    algo = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    public_jwk = algo.to_jwk(rsa_key.public_key(), as_dict=True)
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


@pytest.fixture
def _patch_jwks(monkeypatch, jwks):
    monkeypatch.setattr(helpers, "_http_get_json", lambda uri: jwks)
    helpers._reset_jwks_cache()
    yield
    helpers._reset_jwks_cache()


def _mint(rsa_key, groups):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": "admin-sub", "client_id": AUDIENCE,
        "token_use": "access", "iat": now, "nbf": now - 1, "exp": now + 3600,
        "aud": AUDIENCE,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def cognito_app(fake_table):
    return _app(
        INVITES_TABLE="spec-server-invites",
        COGNITO_ISSUER=ISSUER,
        COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE],
    )


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_non_admin_cannot_mint(cognito_app, rsa_key, _patch_jwks):
    tok = _mint(rsa_key, groups=["spec-writers"])  # write, but not admin
    r = cognito_app.test_client().post("/api/v1/admin/invites", json={}, headers=_auth(tok))
    assert r.status_code == 403, r.get_json()


def test_reader_cannot_list(cognito_app, rsa_key, _patch_jwks):
    tok = _mint(rsa_key, groups=["spec-readers"])  # read would pass a normal GET
    r = cognito_app.test_client().get("/api/v1/admin/invites", headers=_auth(tok))
    assert r.status_code == 403, r.get_json()


def test_missing_token_is_401(cognito_app, _patch_jwks):
    r = cognito_app.test_client().post("/api/v1/admin/invites", json={})
    assert r.status_code == 401


def test_admin_can_mint(cognito_app, rsa_key, _patch_jwks, fake_table):
    tok = _mint(rsa_key, groups=["spec-admins"])
    r = cognito_app.test_client().post("/api/v1/admin/invites", json={}, headers=_auth(tok))
    assert r.status_code == 201, r.get_json()
    assert fake_table.items[0]["status"] == "active"
