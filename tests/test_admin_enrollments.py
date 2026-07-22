"""Admin agent-enrollment endpoint tests (ONBOARD-2):
POST/GET/DELETE /api/v1/admin/agent-enrollments.

Mirrors test_admin_invites.py. Two concerns:

  * Behaviour (auth OFF, the baseline suite's mode): minting stores ONLY the
    SHA-256 token_hash (status active), returns the plaintext token + URL once,
    listing never leaks token material, revoke flips status -> revoked, an
    unconfigured table yields 501, and a bad role is 422.
  * Authz (Cognito ON): a non-admin token is 403; a spec-admins token mints;
    under PROJECT_ISOLATION_ENFORCED a project-admin (the creator, a global
    spec-admin auto-recorded as an admin member) mints for THEIR project while a
    stranger writer is 403. It ALSO documents the CONFIRMED semantics of
    require_project_perm(slug, "admin"): its subsuming global "admin" gate means a
    project-admin member who is only a global spec-*writer* is still 403.

The enrollments table is faked in-memory (monkeypatched into
``app.blueprints.admin._enrollments_table``) so no DynamoDB Local is required.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app, helpers
from app.blueprints import admin as admin_bp
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


class FakeTable:
    """In-memory stand-in for the enrollments DynamoDB Table (resource API)."""

    def __init__(self):
        self.items: list[dict] = []

    def _find(self, token_hash):
        for it in self.items:
            if it.get("token_hash") == token_hash:
                return it
        return None

    def put_item(self, Item, ConditionExpression=None):
        # token_hash is unique in these tests; ignore the collision guard.
        self.items.append(dict(Item))
        return {}

    def scan(self, ExclusiveStartKey=None):
        return {"Items": list(self.items)}

    def get_item(self, Key):
        it = self._find(Key["token_hash"])
        return {"Item": it} if it is not None else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        it = self._find(Key["token_hash"])
        if it is None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        # Minimal "SET #s = :revoked" evaluator (the only form the endpoint uses).
        assert UpdateExpression.strip().startswith("SET ")
        assignment = UpdateExpression.strip()[4:]
        lhs, rhs = (p.strip() for p in assignment.split("="))
        name = (ExpressionAttributeNames or {}).get(lhs, lhs)
        value = (ExpressionAttributeValues or {})[rhs]
        it[name] = value
        return {}


@pytest.fixture
def fake_table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(admin_bp, "_enrollments_table", lambda cfg: t)
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


def _body(**over):
    b = {"project_slug": "demo", "agent_name": "bot-1", "role": "writer"}
    b.update(over)
    return b


# --------------------------------------------------------------------------- #
# Behaviour (auth off)
# --------------------------------------------------------------------------- #
def test_mint_stores_only_hash_active(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="spec-server-agent-enrollments",
               ENROLL_BASE_URL="https://spec.example.com")
    r = app.test_client().post("/api/v1/admin/agent-enrollments", json=_body())
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    token = body["token"]
    assert token and len(token) >= 32
    assert body["enrollment_url"] == f"https://spec.example.com/enroll#token={token}"
    assert body["project_slug"] == "demo"
    assert body["role"] == "writer"
    assert body["agent_name"] == "bot-1"
    assert body["expires_at"] > int(time.time())
    # The mint response NEVER carries the token_hash.
    assert "token_hash" not in body
    # The stored row carries only the HASH + status active — never the plaintext.
    assert len(fake_table.items) == 1
    stored = fake_table.items[0]
    assert stored["token_hash"] == _sha(token)
    assert stored["status"] == "active"
    assert stored["project_slug"] == "demo"
    assert stored["role"] == "writer"
    # The plaintext token appears NOWHERE in the stored row.
    assert token not in stored.values()


def test_mint_default_ttl_and_override(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t", ENROLL_TTL_SECONDS=3600)
    c = app.test_client()
    now = int(time.time())
    # Distinct agent_names so the ONBOARD-3a duplicate-active guard does not fire.
    d = c.post("/api/v1/admin/agent-enrollments",
               json=_body(agent_name="ttl-default")).get_json()
    assert 3590 <= d["expires_at"] - now <= 3610
    o = c.post("/api/v1/admin/agent-enrollments",
               json=_body(agent_name="ttl-override", ttl_seconds=120)).get_json()
    assert 110 <= o["expires_at"] - now <= 130


# --------------------------------------------------------------------------- #
# ONBOARD-3a — reject a second ACTIVE enrollment for the same (project, agent)
# --------------------------------------------------------------------------- #
def test_duplicate_active_enrollment_is_409(fake_table):
    """A second live token for the same (project_slug, agent_name) is refused with
    a generic 409 so two redeems can't race to provision/rotate one Cognito user."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    r1 = c.post("/api/v1/admin/agent-enrollments", json=_body())
    assert r1.status_code == 201, r1.get_json()
    r2 = c.post("/api/v1/admin/agent-enrollments", json=_body())
    assert r2.status_code == 409, r2.get_json()
    # Only the first token was ever stored (the duplicate never minted).
    assert len(fake_table.items) == 1


def test_duplicate_check_is_per_project(fake_table):
    """The SAME agent_name in a DIFFERENT project is allowed — the guard is scoped
    to the (project, agent) pair, matching the per-project isolation intent."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    assert c.post("/api/v1/admin/agent-enrollments",
                  json=_body(project_slug="alpha")).status_code == 201
    assert c.post("/api/v1/admin/agent-enrollments",
                  json=_body(project_slug="beta")).status_code == 201


def test_mint_allowed_after_prior_enrollment_used(fake_table):
    """Once the prior token is redeemed (status 'used') a fresh mint is allowed."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201
    fake_table.items[0]["status"] = "used"
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201


def test_mint_allowed_after_prior_enrollment_expired(fake_table):
    """Once the prior token has expired a fresh mint is allowed."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201
    fake_table.items[0]["expires_at"] = int(time.time()) - 10
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201


def test_mint_allowed_after_prior_enrollment_revoked(fake_table):
    """Once the prior token is revoked a fresh mint is allowed."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201
    fake_table.items[0]["status"] = "revoked"
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 201


def test_list_never_leaks_token_material(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    minted = c.post("/api/v1/admin/agent-enrollments", json=_body()).get_json()
    r = c.get("/api/v1/admin/agent-enrollments")
    assert r.status_code == 200
    rows = r.get_json()
    assert len(rows) == 1
    row = rows[0]
    assert row["project_slug"] == "demo"
    assert row["agent_name"] == "bot-1"
    assert row["role"] == "writer"
    assert row["status"] == "active"
    # The list surfaces the token_hash (the revocation handle / DELETE key) — a
    # one-way hash, NOT the token — so the UI can revoke. The PLAINTEXT token is
    # still never listed.
    assert "token" not in row
    assert row["token_hash"] == _sha(minted["token"])
    assert minted["token"] not in row.values()


def test_list_scoped_to_project(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    c.post("/api/v1/admin/agent-enrollments", json=_body(project_slug="alpha"))
    c.post("/api/v1/admin/agent-enrollments", json=_body(project_slug="beta"))
    rows = c.get("/api/v1/admin/agent-enrollments?project_slug=alpha").get_json()
    assert {r["project_slug"] for r in rows} == {"alpha"}


def test_revoke_flips_status(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    token = c.post("/api/v1/admin/agent-enrollments", json=_body()).get_json()["token"]
    token_hash = _sha(token)
    r = c.delete(f"/api/v1/admin/agent-enrollments/{token_hash}")
    assert r.status_code == 204
    assert fake_table.items[0]["status"] == "revoked"
    # Idempotent: revoking again (already revoked / present) is still 204.
    assert c.delete(f"/api/v1/admin/agent-enrollments/{token_hash}").status_code == 204


def test_list_token_hash_is_a_working_revoke_handle(fake_table):
    """The token_hash surfaced by GET is the exact DELETE key — the UI lists then
    revokes with no other handle (and never needs the plaintext token)."""
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    c = app.test_client()
    c.post("/api/v1/admin/agent-enrollments", json=_body())
    listed_hash = c.get("/api/v1/admin/agent-enrollments").get_json()[0]["token_hash"]
    assert c.delete(f"/api/v1/admin/agent-enrollments/{listed_hash}").status_code == 204
    assert fake_table.items[0]["status"] == "revoked"


def test_revoke_unknown_token_is_idempotent_204(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    r = app.test_client().delete(f"/api/v1/admin/agent-enrollments/{_sha('nope')}")
    assert r.status_code == 204


def test_unconfigured_table_returns_501():
    # AGENT_ENROLLMENTS_TABLE unset (TestConfig default) -> graceful 501, no crash.
    app = _app()
    c = app.test_client()
    assert c.post("/api/v1/admin/agent-enrollments", json=_body()).status_code == 501
    assert c.get("/api/v1/admin/agent-enrollments").status_code == 501
    assert c.delete(f"/api/v1/admin/agent-enrollments/{_sha('x')}").status_code == 501


def test_unconfigured_table_does_not_create_project():
    """ONBOARD-7 regression: when the enrollments table is unset the mint 501s
    BEFORE any storage write — a brand-new slug must NOT be created as an orphan."""
    app = _app()  # AGENT_ENROLLMENTS_TABLE unset
    c = app.test_client()
    slug = f"orphan-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/admin/agent-enrollments",
               json={"project_slug": slug, "agent_name": "bot", "role": "writer"})
    assert r.status_code == 501, r.get_json()
    assert c.get(f"/api/v1/projects/{slug}").status_code == 404


def test_bad_role_is_422(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    r = app.test_client().post("/api/v1/admin/agent-enrollments",
                               json=_body(role="superuser"))
    assert r.status_code == 422, r.get_json()
    assert fake_table.items == []


def test_missing_required_fields_is_422(fake_table):
    app = _app(AGENT_ENROLLMENTS_TABLE="t")
    r = app.test_client().post("/api/v1/admin/agent-enrollments",
                               json={"project_slug": "demo"})
    assert r.status_code == 422, r.get_json()


# --------------------------------------------------------------------------- #
# Authz (Cognito on) — non-admin 403, admin mints, project-admin scoping
# --------------------------------------------------------------------------- #
ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_ENROLLPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "enroll-test-client"
KID = "enroll-key-1"


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


def _mint(rsa_key, *, sub, groups):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": sub, "client_id": AUDIENCE, "aud": AUDIENCE,
        "token_use": "access", "iat": now, "nbf": now - 1, "exp": now + 3600,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def cognito_app(fake_table):
    return _app(
        AGENT_ENROLLMENTS_TABLE="spec-server-agent-enrollments",
        COGNITO_ISSUER=ISSUER,
        COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE],
    )


@pytest.fixture
def cognito_app_enforced(fake_table):
    return _app(
        AGENT_ENROLLMENTS_TABLE="spec-server-agent-enrollments",
        COGNITO_ISSUER=ISSUER,
        COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE],
        PROJECT_ISOLATION_ENFORCED=True,
    )


def _mk_project(client, admin_token, slug):
    r = client.post("/api/v1/projects", json={"slug": slug, "name": "P"},
                    headers=_auth(admin_token))
    assert r.status_code == 201, r.get_json()
    return slug


def test_missing_token_is_401(cognito_app, _patch_jwks):
    r = cognito_app.test_client().post("/api/v1/admin/agent-enrollments", json=_body())
    assert r.status_code == 401


def test_non_admin_writer_cannot_mint(cognito_app, rsa_key, _patch_jwks):
    tok = _mint(rsa_key, sub="w", groups=["spec-writers"])  # write, not admin
    r = cognito_app.test_client().post("/api/v1/admin/agent-enrollments",
                                       json=_body(), headers=_auth(tok))
    assert r.status_code == 403, r.get_json()


def test_reader_cannot_list(cognito_app, rsa_key, _patch_jwks):
    tok = _mint(rsa_key, sub="r", groups=["spec-readers"])
    r = cognito_app.test_client().get("/api/v1/admin/agent-enrollments",
                                      headers=_auth(tok))
    assert r.status_code == 403, r.get_json()


def test_admin_can_mint(cognito_app, rsa_key, _patch_jwks, fake_table):
    tok = _mint(rsa_key, sub="a", groups=["spec-admins"])
    r = cognito_app.test_client().post("/api/v1/admin/agent-enrollments",
                                       json=_body(), headers=_auth(tok))
    assert r.status_code == 201, r.get_json()
    assert fake_table.items[0]["status"] == "active"
    assert fake_table.items[0]["created_by"] == "a"  # verified sub, not body


def test_project_admin_can_mint_for_their_project(cognito_app_enforced, rsa_key,
                                                  _patch_jwks, fake_table):
    """Under enforcement, the project's admin (the creator — a global spec-admin
    auto-recorded as an admin member) mints for THEIR project (201) while a
    stranger writer is 403."""
    c = cognito_app_enforced.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    admin = _mint(rsa_key, sub="creator", groups=["spec-admins"])
    _mk_project(c, admin, slug)

    r = c.post("/api/v1/admin/agent-enrollments",
               json=_body(project_slug=slug), headers=_auth(admin))
    assert r.status_code == 201, r.get_json()

    stranger = _mint(rsa_key, sub="stranger", groups=["spec-writers"])
    r2 = c.post("/api/v1/admin/agent-enrollments",
                json=_body(project_slug=slug), headers=_auth(stranger))
    assert r2.status_code == 403, r2.get_json()


def test_confirmed_semantics_writer_project_admin_still_403(cognito_app_enforced,
                                                            rsa_key, _patch_jwks):
    """CONFIRMED semantics of require_project_perm(slug, 'admin'): its subsuming
    global 'admin' gate requires the caller's GLOBAL groups to grant admin. So a
    caller who is a project-*admin* member but only a global spec-*writer* is
    still 403 — the project-membership branch never runs for the admin perm."""
    c = cognito_app_enforced.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    admin = _mint(rsa_key, sub="creator2", groups=["spec-admins"])
    _mk_project(c, admin, slug)
    # Make the writer a project-ADMIN member.
    r = c.post(f"/api/v1/projects/{slug}/members",
               json={"principal_sub": "pw", "role": "admin"}, headers=_auth(admin))
    assert r.status_code in (200, 201), r.get_json()

    writer = _mint(rsa_key, sub="pw", groups=["spec-writers"])
    r2 = c.post("/api/v1/admin/agent-enrollments",
                json=_body(project_slug=slug), headers=_auth(writer))
    assert r2.status_code == 403, r2.get_json()


# --------------------------------------------------------------------------- #
# ONBOARD-7 — mint auto-creates the project when project_slug is new.
#
# These run cross-backend (postgres + dynamodb) via the parity ``app`` fixture so
# the storage create/exists/race path is proven identically on both adapters. The
# enrollments table itself stays the in-memory ``FakeTable`` (an auth artifact, not
# storage). Auth is OFF here, so the global-admin gate is a no-op and no creator
# member is stamped (creator_sub is None) — the Cognito cases below prove the
# authz + creator-becomes-admin-member invariants.
# --------------------------------------------------------------------------- #
def _wire_enrollments(app, monkeypatch):
    """Point the mint endpoint at a fresh in-memory FakeTable for a parity app."""
    t = FakeTable()
    monkeypatch.setattr(admin_bp, "_enrollments_table", lambda cfg: t)
    monkeypatch.setitem(app.config, "AGENT_ENROLLMENTS_TABLE", "t")
    return t


def test_mint_creates_missing_project(app, monkeypatch):
    ft = _wire_enrollments(app, monkeypatch)
    c = app.test_client()
    slug = f"newproj-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/admin/agent-enrollments",
               json={"project_slug": slug, "agent_name": "bot", "role": "writer"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["project_created"] is True
    assert body["token"]  # the token is still issued once
    assert body["project_slug"] == slug
    # The project now exists and is readable.
    pr = c.get(f"/api/v1/projects/{slug}")
    assert pr.status_code == 200, pr.get_json()
    # The enrollment token was stored (active).
    assert len(ft.items) == 1 and ft.items[0]["status"] == "active"


def test_mint_derives_display_name_from_slug(app, monkeypatch):
    _wire_enrollments(app, monkeypatch)
    c = app.test_client()
    slug = f"bird-viz-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/admin/agent-enrollments",
               json={"project_slug": slug, "agent_name": "bot", "role": "writer"})
    assert r.status_code == 201, r.get_json()
    # dashes/underscores -> spaces, title-cased.
    name = c.get(f"/api/v1/projects/{slug}").get_json()["name"]
    assert name == slug.replace("-", " ").title()


def test_mint_uses_supplied_project_name(app, monkeypatch):
    _wire_enrollments(app, monkeypatch)
    c = app.test_client()
    slug = f"named-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/admin/agent-enrollments",
               json={"project_slug": slug, "agent_name": "bot", "role": "writer",
                     "project_name": "My Fancy Project"})
    assert r.status_code == 201, r.get_json()
    assert c.get(f"/api/v1/projects/{slug}").get_json()["name"] == "My Fancy Project"


def test_mint_existing_project_reports_not_created(app, monkeypatch):
    ft = _wire_enrollments(app, monkeypatch)
    c = app.test_client()
    slug = f"exists-{uuid.uuid4().hex[:8]}"
    # Pre-create the project (unchanged path).
    assert c.post("/api/v1/projects",
                  json={"slug": slug, "name": "Preexisting"}).status_code == 201
    r = c.post("/api/v1/admin/agent-enrollments",
               json={"project_slug": slug, "agent_name": "bot", "role": "writer"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["project_created"] is False
    assert body["token"]
    # The pre-existing name was NOT overwritten.
    assert c.get(f"/api/v1/projects/{slug}").get_json()["name"] == "Preexisting"
    assert len(ft.items) == 1 and ft.items[0]["status"] == "active"


def test_concurrent_same_new_slug_mints_one_creates(app, monkeypatch):
    """Two concurrent mints for the SAME brand-new slug: exactly one creates the
    project, the other catches the storage Conflict and proceeds to mint (never a
    500). Cross-backend via the parity app. Distinct agent_names so the per-(project,
    agent) dedupe guard does not turn the second mint into a 409."""
    _wire_enrollments(app, monkeypatch)
    slug = f"race-{uuid.uuid4().hex[:8]}"
    results: list = []
    lock = threading.Lock()

    def worker(idx):
        r = app.test_client().post(
            "/api/v1/admin/agent-enrollments",
            json={"project_slug": slug, "agent_name": f"bot-{idx}", "role": "writer"},
        )
        with lock:
            results.append((r.status_code, r.get_json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = [sc for sc, _ in results]
    assert all(sc == 201 for sc in codes), results  # never a 500
    created_flags = [body["project_created"] for _, body in results]
    assert sorted(created_flags) == [False, True]  # exactly one creator
    assert app.test_client().get(f"/api/v1/projects/{slug}").status_code == 200


# --------------------------------------------------------------------------- #
# ONBOARD-7 authz (Cognito on) — only a GLOBAL admin can create-via-mint, and the
# VERIFIED caller becomes the new project's admin member.
# --------------------------------------------------------------------------- #
def test_admin_create_via_mint_records_caller_as_admin_member(
    cognito_app_enforced, rsa_key, _patch_jwks, fake_table
):
    c = cognito_app_enforced.test_client()
    slug = f"onb7-{uuid.uuid4().hex[:8]}"
    admin = _mint(rsa_key, sub="founder", groups=["spec-admins"])
    r = c.post("/api/v1/admin/agent-enrollments",
               json=_body(project_slug=slug), headers=_auth(admin))
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["project_created"] is True
    # The VERIFIED caller (sub 'founder'), not a body value, is the admin member.
    members = c.get(f"/api/v1/projects/{slug}/members", headers=_auth(admin)).get_json()
    assert any(m["principal_sub"] == "founder" and m["role"] == "admin" for m in members)


def test_non_admin_cannot_create_via_mint(cognito_app, rsa_key, _patch_jwks, fake_table):
    """A non-admin (writer) cannot create a project through the mint path: 403 and
    the project is NOT created."""
    c = cognito_app.test_client()
    slug = f"onb7-deny-{uuid.uuid4().hex[:8]}"
    writer = _mint(rsa_key, sub="w", groups=["spec-writers"])
    r = c.post("/api/v1/admin/agent-enrollments",
               json=_body(project_slug=slug), headers=_auth(writer))
    assert r.status_code == 403, r.get_json()
    assert fake_table.items == []  # nothing minted
    # The project was NOT created (an admin read still 404s it).
    admin = _mint(rsa_key, sub="a", groups=["spec-admins"])
    assert c.get(f"/api/v1/projects/{slug}", headers=_auth(admin)).status_code == 404
