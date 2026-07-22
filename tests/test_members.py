"""HTTP surface + admin gating for project-membership management (ISO-3).

Two groups of tests:

* **Behaviour (auth off)** — driven through the HTTP API via the shared ``client``
  / ``project`` fixtures, which are parametrised over BOTH storage backends
  (Postgres reference + DynamoDB Local), so add -> list -> delete, 404, 422 and
  idempotency are proven identical on both adapters (the SLS-8 parity rule).

* **Admin gating** — the three routes are gated on the GLOBAL admin permission
  (``spec-admins``). These build a self-contained Cognito-JWT app (mirroring
  ``tests/test_auth.py``) and assert a non-admin token is 403 while an admin
  token succeeds, and — critically — that the authorization decision keys ONLY
  off the caller's verified token, never off the ``role`` in the request body.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app, helpers
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL


# =========================================================================== #
# Behaviour over HTTP (auth off) — runs on BOTH backends via the app fixture.
# =========================================================================== #
def test_add_list_delete_roundtrip(client, project):
    """POST creates (201), GET lists it, DELETE removes it (204), GET is empty."""
    assert client.get("/api/v1/projects/demo/members").get_json() == []

    r = client.post("/api/v1/projects/demo/members",
                     json={"principal_sub": "sub-1", "principal_name": "Alice",
                           "role": "reader"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["principal_sub"] == "sub-1"
    assert body["principal_name"] == "Alice"
    assert body["role"] == "reader"
    assert body["project_slug"] == "demo"

    listing = client.get("/api/v1/projects/demo/members").get_json()
    assert [m["principal_sub"] for m in listing] == ["sub-1"]

    d = client.delete("/api/v1/projects/demo/members/sub-1")
    assert d.status_code == 204
    assert client.get("/api/v1/projects/demo/members").get_json() == []


def test_post_is_idempotent_upsert_200_on_update(client, project):
    """A first POST is 201 (create); re-POSTing the same principal is 200
    (update), changes role/name in place, and never creates a second row."""
    r1 = client.post("/api/v1/projects/demo/members",
                      json={"principal_sub": "sub-x", "role": "reader"})
    assert r1.status_code == 201, r1.get_json()

    r2 = client.post("/api/v1/projects/demo/members",
                      json={"principal_sub": "sub-x", "principal_name": "Xavier",
                            "role": "admin"})
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_json()["role"] == "admin"
    assert r2.get_json()["principal_name"] == "Xavier"

    listing = client.get("/api/v1/projects/demo/members").get_json()
    assert len(listing) == 1
    assert listing[0]["role"] == "admin"


def test_delete_is_idempotent(client, project):
    """Deleting an absent (but never-added) member on an existing project is a
    no-op -> 204."""
    assert client.delete("/api/v1/projects/demo/members/ghost").status_code == 204


def test_unknown_project_is_404(client):
    """Every route 404s when the project does not exist (storage NotFound)."""
    assert client.get("/api/v1/projects/nope/members").status_code == 404
    r = client.post("/api/v1/projects/nope/members",
                    json={"principal_sub": "s", "role": "reader"})
    assert r.status_code == 404
    assert client.delete("/api/v1/projects/nope/members/s").status_code == 404


def test_bad_role_is_422(client, project):
    """A role outside reader/writer/admin is rejected by MemberIn -> 422."""
    r = client.post("/api/v1/projects/demo/members",
                    json={"principal_sub": "s", "role": "superuser"})
    assert r.status_code == 422, r.get_json()


def test_missing_required_fields_is_422(client, project):
    """principal_sub and role are required."""
    assert client.post("/api/v1/projects/demo/members",
                       json={"role": "reader"}).status_code == 422
    assert client.post("/api/v1/projects/demo/members",
                       json={"principal_sub": "s"}).status_code == 422


# =========================================================================== #
# Admin gating (Cognito JWT) — self-contained app, mirrors tests/test_auth.py.
# These build their own app instances, so the backend-parametrised suite and the
# session-scoped auth-off app are untouched.
# =========================================================================== #
ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_MEMBERPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "test-members-client-id"
KID = "members-key-1"
GROUP_READ = "spec-readers"
GROUP_WRITE = "spec-writers"
GROUP_ADMIN = "spec-admins"


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


def _mint(rsa_key, *, groups=(GROUP_ADMIN,)):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": "caller-sub", "client_id": AUDIENCE,
        "aud": AUDIENCE, "token_use": "access",
        "iat": now, "nbf": now - 1, "exp": now + 3600,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cognito_app():
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        COGNITO_ISSUER = ISSUER
        COGNITO_JWKS_URI = JWKS_URI
        COGNITO_AUDIENCE = [AUDIENCE]

    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


def _mk_project(client, admin):
    slug = f"m-{uuid.uuid4().hex[:10]}"
    assert client.post("/api/v1/projects", json={"slug": slug, "name": "M"},
                       headers=_auth(admin)).status_code == 201
    return slug


def test_reader_token_cannot_list_members_403(cognito_app, rsa_key):
    """Listing members needs admin, NOT the default ``read`` a GET usually needs:
    a reader token is 403."""
    c = cognito_app.test_client()
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    r = c.get(f"/api/v1/projects/{slug}/members",
              headers=_auth(_mint(rsa_key, groups=[GROUP_READ])))
    assert r.status_code == 403
    assert r.get_json()["code"] == 403


def test_writer_token_cannot_add_member_403(cognito_app, rsa_key):
    c = cognito_app.test_client()
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    r = c.post(f"/api/v1/projects/{slug}/members",
               json={"principal_sub": "s", "role": "reader"},
               headers=_auth(_mint(rsa_key, groups=[GROUP_WRITE])))
    assert r.status_code == 403


def test_missing_token_is_401(cognito_app, rsa_key):
    c = cognito_app.test_client()
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    assert c.get(f"/api/v1/projects/{slug}/members").status_code == 401


def test_admin_token_can_manage_members(cognito_app, rsa_key):
    """An admin token can list, add and delete members end-to-end."""
    c = cognito_app.test_client()
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)

    assert c.get(f"/api/v1/projects/{slug}/members",
                 headers=_auth(admin)).status_code == 200
    r = c.post(f"/api/v1/projects/{slug}/members",
               json={"principal_sub": "sub-1", "role": "writer"},
               headers=_auth(admin))
    assert r.status_code == 201, r.get_json()
    assert c.delete(f"/api/v1/projects/{slug}/members/sub-1",
                    headers=_auth(admin)).status_code == 204


def test_body_role_admin_does_not_authorize_a_non_admin_caller(cognito_app, rsa_key):
    """SECURITY: authorization keys off the caller's VERIFIED token only. A
    non-admin caller sending ``role: admin`` (the TARGET member's role) in the
    body is still 403 — the body never elevates the caller."""
    c = cognito_app.test_client()
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    r = c.post(f"/api/v1/projects/{slug}/members",
               json={"principal_sub": "attacker", "principal_name": "me",
                     "role": "admin"},
               headers=_auth(_mint(rsa_key, groups=[GROUP_READ])))
    assert r.status_code == 403
