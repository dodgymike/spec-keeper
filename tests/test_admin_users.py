"""Admin user-lifecycle endpoint tests (HA-5).

Covers GET /api/v1/admin/users and the approve/reject/block/unblock/promote/
demote/delete actions. The Cognito pool is faked in-memory (monkeypatched into
``app.blueprints.admin._cognito_client``) so no real Cognito is required. Authz
tests reuse the in-process RSA/JWKS harness (mirrors test_admin_invites).
"""
from __future__ import annotations

import datetime as dt
import time

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app, helpers
from app.blueprints import admin as admin_bp
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL

POOL_ID = "us-east-1_TESTPOOL"


class FakeCognito:
    """In-memory stand-in for a boto3 cognito-idp client (subset used by HA-5)."""

    def __init__(self):
        # username -> {"attributes": {...}, "enabled": bool, "groups": set, "created": dt}
        self.users: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def add_user(self, username, *, email=None, sub=None, groups=(), enabled=True):
        attrs = {}
        if email is not None:
            attrs["email"] = email
        if sub is not None:
            attrs["sub"] = sub
        self.users[username] = {
            "attributes": attrs,
            "enabled": enabled,
            "groups": set(groups),
            "created": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        }

    def _u(self, username):
        if username not in self.users:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "not found"}},
                "AdminGetUser",
            )
        return self.users[username]

    def _attr_list(self, u):
        return [{"Name": k, "Value": v} for k, v in u["attributes"].items()]

    def admin_get_user(self, UserPoolId, Username):
        u = self._u(Username)
        return {"Username": Username, "Enabled": u["enabled"],
                "UserAttributes": self._attr_list(u), "UserCreateDate": u["created"]}

    def list_users(self, UserPoolId, Limit=None, PaginationToken=None):
        return {"Users": [
            {"Username": name, "Enabled": u["enabled"],
             "Attributes": self._attr_list(u), "UserCreateDate": u["created"]}
            for name, u in self.users.items()
        ]}

    def admin_list_groups_for_user(self, UserPoolId, Username, Limit=None, NextToken=None):
        u = self._u(Username)
        return {"Groups": [{"GroupName": g} for g in sorted(u["groups"])]}

    def list_users_in_group(self, UserPoolId, GroupName, Limit=None, NextToken=None):
        return {"Users": [
            {"Username": name} for name, u in self.users.items() if GroupName in u["groups"]
        ]}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self._u(Username)["groups"].add(GroupName)
        self.calls.append(("add", Username, GroupName))

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self._u(Username)["groups"].discard(GroupName)
        self.calls.append(("remove", Username, GroupName))

    def admin_disable_user(self, UserPoolId, Username):
        self._u(Username)["enabled"] = False
        self.calls.append(("disable", Username))

    def admin_enable_user(self, UserPoolId, Username):
        self._u(Username)["enabled"] = True
        self.calls.append(("enable", Username))

    def admin_delete_user(self, UserPoolId, Username):
        self._u(Username)
        del self.users[Username]
        self.calls.append(("delete", Username))


@pytest.fixture
def fake_cognito(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(admin_bp, "_cognito_client", lambda cfg: fake)
    return fake


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
# Behaviour (auth off) — actions operate on the fake pool
# --------------------------------------------------------------------------- #
def test_unconfigured_pool_returns_501():
    app = _app()  # COGNITO_USER_POOL_ID unset
    r = app.test_client().get("/api/v1/admin/users")
    assert r.status_code == 501


def test_list_users_status_filter(fake_cognito):
    fake_cognito.add_user("pend@x.io", email="pend@x.io", groups=[])
    fake_cognito.add_user("act@x.io", email="act@x.io", groups=["spec-readers"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    c = app.test_client()

    allr = c.get("/api/v1/admin/users").get_json()
    assert {u["username"]: u["status"] for u in allr} == {
        "pend@x.io": "pending", "act@x.io": "active"
    }
    pend = c.get("/api/v1/admin/users?status=pending").get_json()
    assert [u["username"] for u in pend] == ["pend@x.io"]
    assert pend[0]["created_at"].startswith("2026-01-01")


def test_approve_adds_default_readers(fake_cognito):
    fake_cognito.add_user("new@x.io", email="new@x.io", groups=[])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/new@x.io/approve", json={})
    assert r.status_code == 204
    assert fake_cognito.users["new@x.io"]["groups"] == {"spec-readers"}


def test_approve_can_grant_writers(fake_cognito):
    fake_cognito.add_user("new@x.io", email="new@x.io", groups=[])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post(
        "/api/v1/admin/users/new@x.io/approve", json={"group": "spec-writers"}
    )
    assert r.status_code == 204
    assert fake_cognito.users["new@x.io"]["groups"] == {"spec-writers"}


def test_block_disables_and_strips_groups(fake_cognito):
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=["spec-writers", "spec-readers"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/u@x.io/block", json={})
    assert r.status_code == 204
    u = fake_cognito.users["u@x.io"]
    assert u["enabled"] is False
    assert u["groups"] == set()


def test_unblock_reenables(fake_cognito):
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=[], enabled=False)
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/u@x.io/unblock", json={})
    assert r.status_code == 204
    assert fake_cognito.users["u@x.io"]["enabled"] is True


def test_promote_adds_admins(fake_cognito):
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=["spec-writers"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/u@x.io/promote", json={})
    assert r.status_code == 204
    assert "spec-admins" in fake_cognito.users["u@x.io"]["groups"]


def test_demote_removes_admins_when_not_last(fake_cognito):
    fake_cognito.add_user("a@x.io", email="a@x.io", groups=["spec-admins"])
    fake_cognito.add_user("b@x.io", email="b@x.io", groups=["spec-admins"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/a@x.io/demote", json={})
    assert r.status_code == 204
    assert "spec-admins" not in fake_cognito.users["a@x.io"]["groups"]


def test_demote_last_admin_refused(fake_cognito):
    fake_cognito.add_user("solo@x.io", email="solo@x.io", groups=["spec-admins"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/solo@x.io/demote", json={})
    assert r.status_code == 409
    assert "spec-admins" in fake_cognito.users["solo@x.io"]["groups"]


def test_delete_removes_user(fake_cognito):
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=[])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().delete("/api/v1/admin/users/u@x.io")
    assert r.status_code == 204
    assert "u@x.io" not in fake_cognito.users


def test_unknown_user_404(fake_cognito):
    app = _app(COGNITO_USER_POOL_ID=POOL_ID)
    r = app.test_client().post("/api/v1/admin/users/ghost@x.io/approve", json={})
    assert r.status_code == 404


def test_self_guarded_mutation_fails_closed_under_static_keys(fake_cognito):
    # API_KEYS auth (no COGNITO_ISSUER) => current_identity() is blind, so the
    # self-lockout guard can't protect the caller: block/delete/demote 501 rather
    # than run the guard blind. Approve (not self-protected) still works.
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=["spec-admins"])
    app = _app(COGNITO_USER_POOL_ID=POOL_ID, API_KEYS=["k"])
    hdr = {"Authorization": "Bearer k"}
    c = app.test_client()
    assert c.post("/api/v1/admin/users/u@x.io/block", json={}, headers=hdr).status_code == 501
    assert c.delete("/api/v1/admin/users/u@x.io", headers=hdr).status_code == 501
    assert c.post("/api/v1/admin/users/u@x.io/demote", json={}, headers=hdr).status_code == 501
    # Non-self-protected actions remain available under static-key auth.
    assert c.post("/api/v1/admin/users/u@x.io/approve", json={}, headers=hdr).status_code == 204


# --------------------------------------------------------------------------- #
# Authz + self-lockout guardrail (Cognito on)
# --------------------------------------------------------------------------- #
ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_USERSPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "users-test-client"
KID = "users-key-1"
CALLER_SUB = "caller-sub-123"
CALLER_USERNAME = "me@x.io"


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


def _mint(rsa_key, groups, *, sub=CALLER_SUB, username=CALLER_USERNAME):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": sub, "username": username, "client_id": AUDIENCE,
        "token_use": "access", "iat": now, "nbf": now - 1, "exp": now + 3600,
        "aud": AUDIENCE,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def cognito_app(fake_cognito):
    return _app(
        COGNITO_USER_POOL_ID=POOL_ID,
        COGNITO_ISSUER=ISSUER,
        COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE],
    )


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_non_admin_cannot_list(cognito_app, rsa_key, _patch_jwks):
    tok = _mint(rsa_key, groups=["spec-writers"])
    r = cognito_app.test_client().get("/api/v1/admin/users", headers=_auth(tok))
    assert r.status_code == 403


def test_missing_token_401(cognito_app, _patch_jwks):
    r = cognito_app.test_client().get("/api/v1/admin/users")
    assert r.status_code == 401


def test_admin_can_list(cognito_app, rsa_key, _patch_jwks, fake_cognito):
    fake_cognito.add_user("u@x.io", email="u@x.io", groups=["spec-readers"])
    tok = _mint(rsa_key, groups=["spec-admins"])
    r = cognito_app.test_client().get("/api/v1/admin/users", headers=_auth(tok))
    assert r.status_code == 200
    assert r.get_json()[0]["username"] == "u@x.io"


def test_self_block_refused(cognito_app, rsa_key, _patch_jwks, fake_cognito):
    # Target user's sub attribute matches the caller's token sub -> self-action.
    fake_cognito.add_user(CALLER_USERNAME, email=CALLER_USERNAME, sub=CALLER_SUB,
                          groups=["spec-admins"])
    tok = _mint(rsa_key, groups=["spec-admins"])
    r = cognito_app.test_client().post(
        f"/api/v1/admin/users/{CALLER_USERNAME}/block", json={}, headers=_auth(tok)
    )
    assert r.status_code == 409
    assert fake_cognito.users[CALLER_USERNAME]["enabled"] is True


def test_self_delete_refused(cognito_app, rsa_key, _patch_jwks, fake_cognito):
    fake_cognito.add_user(CALLER_USERNAME, email=CALLER_USERNAME, sub=CALLER_SUB,
                          groups=["spec-admins"])
    tok = _mint(rsa_key, groups=["spec-admins"])
    r = cognito_app.test_client().delete(
        f"/api/v1/admin/users/{CALLER_USERNAME}", headers=_auth(tok)
    )
    assert r.status_code == 409
    assert CALLER_USERNAME in fake_cognito.users


def test_self_demote_refused(cognito_app, rsa_key, _patch_jwks, fake_cognito):
    fake_cognito.add_user(CALLER_USERNAME, email=CALLER_USERNAME, sub=CALLER_SUB,
                          groups=["spec-admins"])
    fake_cognito.add_user("other@x.io", email="other@x.io", groups=["spec-admins"])
    tok = _mint(rsa_key, groups=["spec-admins"])
    r = cognito_app.test_client().post(
        f"/api/v1/admin/users/{CALLER_USERNAME}/demote", json={}, headers=_auth(tok)
    )
    assert r.status_code == 409
    assert "spec-admins" in fake_cognito.users[CALLER_USERNAME]["groups"]
