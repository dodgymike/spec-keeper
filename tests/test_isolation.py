"""ISO-4: per-project authorization (``require_project_perm``) + creator-auto-admin,
behind the default-OFF ``PROJECT_ISOLATION_ENFORCED`` flag.

These tests exercise the JWT path WITHOUT real Cognito (mirroring ``test_auth.py``
/ ``test_members.py``): an in-process RSA keypair signs access tokens carrying a
``sub`` and a ``cognito:groups`` claim, and the JWKS fetch is monkeypatched. That
is the ONLY way to give the server a VERIFIED caller identity — the whole point of
ISO-4 is that authorization keys off the verified token, never a body/header.

Everything is proven on BOTH storage backends (Postgres + DynamoDB Local, the
SLS-8 parity rule) and across BOTH flag states:

* **flag OFF (dormant)** — membership is ignored: a non-member reads/writes exactly
  as today, ``GET /projects`` lists everything. (Byte-for-byte-unchanged proof.)
  Creator-auto-admin STILL records the creator as an ``admin`` member (it runs
  regardless of the flag, so the backlog is ready before the flip).
* **flag ON (enforced)** — a member with a sufficient role → 200; a non-member GET
  → 404 (existence hidden); a non-member/insufficient-role write → 403; a global
  ``spec-admins`` token bypasses; ``GET /projects`` is filtered to memberships;
  ``create_project`` makes the creator an ``admin`` member, atomically (a duplicate
  create never leaves a second orphan member).
"""
from __future__ import annotations

import os
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app, helpers
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL, _create_dynamo_table, _dynamo_clients

ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_ISOPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "iso-client-id"
KID = "iso-key-1"
GROUP_READ = "spec-readers"
GROUP_WRITE = "spec-writers"
GROUP_ADMIN = "spec-admins"

_BACKENDS = [b.strip() for b in os.environ.get("TEST_BACKENDS", "postgres").split(",")
             if b.strip()]


# --------------------------------------------------------------------------- #
# Key material + JWKS (module-scoped; auto-patched onto the fetch)
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


def _mint(rsa_key, *, sub, groups):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": sub, "client_id": AUDIENCE, "aud": AUDIENCE,
        "token_use": "access", "iat": now, "nbf": now - 1, "exp": now + 3600,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# App factory: a Cognito-auth app on a given backend + flag state, self-built so
# the backend-parametrised session app (and the auth-off suite) is untouched.
# --------------------------------------------------------------------------- #
def _build_app(backend, enforced):
    if backend == "postgres":
        class _Cfg(TestConfig):
            STORAGE_BACKEND = "postgres"
            SQLALCHEMY_DATABASE_URI = TEST_DB_URL
            COGNITO_ISSUER = ISSUER
            COGNITO_JWKS_URI = JWKS_URI
            COGNITO_AUDIENCE = [AUDIENCE]
            PROJECT_ISOLATION_ENFORCED = enforced

        app = create_app(_Cfg)
        with app.app_context():
            db.drop_all()
            db.create_all()
        app._backend = "postgres"
        app._teardown = lambda: _pg_teardown(app)
        return app

    endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("DynamoDB Local not configured (set DYNAMODB_ENDPOINT_URL).")
    region = os.environ.get("AWS_REGION", "us-east-1")
    table_name = f"spec-iso-{uuid.uuid4().hex[:8]}"
    os.environ["DYNAMODB_TABLE"] = table_name
    client, resource = _dynamo_clients(endpoint, region)
    _create_dynamo_table(client, table_name)

    class _Cfg(TestConfig):
        STORAGE_BACKEND = "dynamodb"
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL  # unused by the dynamo adapter
        COGNITO_ISSUER = ISSUER
        COGNITO_JWKS_URI = JWKS_URI
        COGNITO_AUDIENCE = [AUDIENCE]
        PROJECT_ISOLATION_ENFORCED = enforced

    app = create_app(_Cfg)
    app._backend = "dynamodb"
    app._teardown = lambda: client.delete_table(TableName=table_name)
    return app


def _pg_teardown(app):
    with app.app_context():
        db.drop_all()


@pytest.fixture(params=_BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
def app_off(backend):
    app = _build_app(backend, enforced=False)
    yield app
    app._teardown()


@pytest.fixture
def app_on(backend):
    app = _build_app(backend, enforced=True)
    yield app
    app._teardown()


# Common principals
ADMIN_SUB = "admin-sub"


def _mk_project(client, admin_token, slug=None):
    slug = slug or f"iso-{uuid.uuid4().hex[:10]}"
    r = client.post("/api/v1/projects", json={"slug": slug, "name": "P"},
                    headers=_auth(admin_token))
    assert r.status_code == 201, r.get_json()
    return slug


def _add_member(client, admin_token, slug, sub, role):
    r = client.post(f"/api/v1/projects/{slug}/members",
                    json={"principal_sub": sub, "role": role},
                    headers=_auth(admin_token))
    assert r.status_code in (200, 201), r.get_json()


# =========================================================================== #
# Flag OFF — DORMANT: membership ignored, behaviour == today.
# =========================================================================== #
def test_off_nonmember_reads_and_writes_like_today(app_off, rsa_key):
    """With the flag OFF, a caller who is NOT a member reads and writes a project
    exactly as the global group model allowed before ISO-4."""
    c = app_off.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)

    reader = _mint(rsa_key, sub="stranger-r", groups=[GROUP_READ])
    writer = _mint(rsa_key, sub="stranger-w", groups=[GROUP_WRITE])

    # Non-member reads succeed (200), as today.
    assert c.get(f"/api/v1/projects/{slug}", headers=_auth(reader)).status_code == 200
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(reader)).status_code == 200
    # Non-member write succeeds (201), as today (global write gate only).
    r = c.post(f"/api/v1/projects/{slug}/tasks",
               json={"title": "T"}, headers=_auth(writer))
    assert r.status_code == 201, r.get_json()


def test_off_list_projects_shows_all(app_off, rsa_key):
    """Flag OFF: GET /projects lists every project regardless of membership."""
    c = app_off.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    a = _mk_project(c, admin)
    b = _mk_project(c, admin)
    reader = _mint(rsa_key, sub="stranger-r", groups=[GROUP_READ])
    slugs = {p["slug"] for p in c.get("/api/v1/projects", headers=_auth(reader)).get_json()}
    assert {a, b} <= slugs


def test_off_creator_is_recorded_as_admin_member(app_off, rsa_key):
    """Creator-auto-admin runs REGARDLESS of the flag: even OFF, the creator is an
    ``admin`` member (so the backlog is ready to enforce when the flag flips)."""
    c = app_off.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    members = c.get(f"/api/v1/projects/{slug}/members", headers=_auth(admin)).get_json()
    by_sub = {m["principal_sub"]: m for m in members}
    assert by_sub.get(ADMIN_SUB, {}).get("role") == "admin"


# =========================================================================== #
# Flag ON — ENFORCED.
# =========================================================================== #
def test_on_member_reader_can_read(app_on, rsa_key):
    """A reader member GETs the project and its tasks (200)."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    _add_member(c, admin, slug, "reader-sub", "reader")
    reader = _mint(rsa_key, sub="reader-sub", groups=[GROUP_READ])
    assert c.get(f"/api/v1/projects/{slug}", headers=_auth(reader)).status_code == 200
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(reader)).status_code == 200


def test_on_nonmember_get_is_404(app_on, rsa_key):
    """A non-member GET 404s — existence hidden, byte-identical to a genuinely
    missing project (same code/status/message envelope)."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    reader = _mint(rsa_key, sub="stranger-r", groups=[GROUP_READ])

    denied = c.get(f"/api/v1/projects/{slug}", headers=_auth(reader))
    missing = c.get("/api/v1/projects/does-not-exist", headers=_auth(reader))
    assert denied.status_code == 404
    assert missing.status_code == 404
    # Indistinguishable: same envelope shape, and same "not found" message form.
    assert denied.get_json()["code"] == missing.get_json()["code"] == 404
    assert denied.get_json()["message"] == f"Project '{slug}' not found."
    # Task listing on a project you're not a member of is likewise hidden -> 404.
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(reader)).status_code == 404


def test_on_nonmember_changes_feed_is_404(app_on, rsa_key):
    """UI-DELTA-6 isolation: the delta feed and head cursor are per-project reads,
    gated exactly like the other project reads. A non-member is 404 (existence
    hidden) — never a cross-project change leak — while a reader member is served."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)

    stranger = _mint(rsa_key, sub="stranger-r", groups=[GROUP_READ])
    assert c.get(f"/api/v1/projects/{slug}/changes", headers=_auth(stranger)).status_code == 404
    assert c.get(f"/api/v1/projects/{slug}/changes/head", headers=_auth(stranger)).status_code == 404

    _add_member(c, admin, slug, "reader-sub", "reader")
    member = _mint(rsa_key, sub="reader-sub", groups=[GROUP_READ])
    assert c.get(f"/api/v1/projects/{slug}/changes", headers=_auth(member)).status_code == 200
    assert c.get(f"/api/v1/projects/{slug}/changes/head", headers=_auth(member)).status_code == 200


def test_on_nonmember_write_is_403(app_on, rsa_key):
    """A non-member with global write capability is 403 on a write to an existing
    project (a write reveals the project exists but denies access)."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    writer = _mint(rsa_key, sub="stranger-w", groups=[GROUP_WRITE])
    r = c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "T"},
               headers=_auth(writer))
    assert r.status_code == 403, r.get_json()


def test_on_writer_member_can_write(app_on, rsa_key):
    """A writer member creates a task (201)."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    _add_member(c, admin, slug, "writer-sub", "writer")
    writer = _mint(rsa_key, sub="writer-sub", groups=[GROUP_WRITE])
    r = c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "T"},
               headers=_auth(writer))
    assert r.status_code == 201, r.get_json()


def test_on_reader_member_cannot_write_403(app_on, rsa_key):
    """Role gates the permission: a caller with global WRITE capability but only a
    project ``reader`` role is 403 on a write (the per-project role is insufficient
    even though the global gate passes)."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    _add_member(c, admin, slug, "ro-sub", "reader")
    # Global spec-writers (passes the global write gate) but project role reader.
    tok = _mint(rsa_key, sub="ro-sub", groups=[GROUP_WRITE])
    r = c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "T"}, headers=_auth(tok))
    assert r.status_code == 403, r.get_json()
    # ...but the same caller CAN read (reader role grants read).
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(tok)).status_code == 200


def test_on_paginated_task_list_read_is_consistent_for_members(app_on, rsa_key):
    """ISO-10: a paginated task-list read is gated on exactly ``read`` — the SAME
    permission as a ``?limit=1`` read — with NO difference between ``?limit=1`` and
    ``?limit=N&offset=M``.

    A ``reader`` AND a ``writer`` member each get 200 on both forms (writer has
    read+write, so it must never be denied a read); a non-member is hidden (404 on
    the paginated read, existence not leaked); and writes still require write
    (writer 200 create, reader 403 create). This locks the guarantee that the
    list endpoint never diverges into a stricter perm for the paginated branch."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    # Enough tasks that offset/limit actually paginate a non-trivial window.
    for i in range(5):
        assert c.post(f"/api/v1/projects/{slug}/tasks", json={"title": f"T{i}"},
                      headers=_auth(admin)).status_code == 201
    _add_member(c, admin, slug, "reader-sub", "reader")
    _add_member(c, admin, slug, "writer-sub", "writer")
    reader = _mint(rsa_key, sub="reader-sub", groups=[GROUP_READ])
    writer = _mint(rsa_key, sub="writer-sub", groups=[GROUP_WRITE])

    for tok in (reader, writer):
        # A single-item read and a paginated window are BOTH 200 and BOTH return
        # tasks — identical gating, no stricter perm on the paginated branch.
        small = c.get(f"/api/v1/projects/{slug}/tasks?limit=1", headers=_auth(tok))
        paged = c.get(f"/api/v1/projects/{slug}/tasks?limit=2&offset=1", headers=_auth(tok))
        assert small.status_code == 200, small.get_json()
        assert paged.status_code == 200, paged.get_json()
        assert len(small.get_json()) == 1
        assert len(paged.get_json()) == 2

    # A non-member is still hidden on the paginated read (404, not 403/200).
    stranger = _mint(rsa_key, sub="nobody", groups=[GROUP_READ])
    assert c.get(f"/api/v1/projects/{slug}/tasks?limit=2&offset=1",
                 headers=_auth(stranger)).status_code == 404

    # Writes are unchanged: writer member creates (201), reader member is 403.
    assert c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "W"},
                  headers=_auth(writer)).status_code == 201
    assert c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "R"},
                  headers=_auth(reader)).status_code == 403


def test_on_spec_admin_bypasses_membership(app_on, rsa_key):
    """A platform super-admin (spec-admins) reaches any project without being a
    member: read 200, write 201, and project-admin ops succeed."""
    c = app_on.test_client()
    creator = _mint(rsa_key, sub="creator-a", groups=[GROUP_ADMIN])
    slug = _mk_project(c, creator)
    # A DIFFERENT spec-admin, not a member of this project.
    other = _mint(rsa_key, sub="other-admin", groups=[GROUP_ADMIN])
    assert c.get(f"/api/v1/projects/{slug}", headers=_auth(other)).status_code == 200
    assert c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "T"},
                  headers=_auth(other)).status_code == 201
    assert c.patch(f"/api/v1/projects/{slug}", json={"name": "N"},
                   headers=_auth(other)).status_code == 200


def test_on_list_projects_is_filtered_to_memberships(app_on, rsa_key):
    """Flag ON: a non-admin sees only projects they belong to; a spec-admin sees
    all; a member-of-nothing sees an empty list."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    a = _mk_project(c, admin)
    b = _mk_project(c, admin)
    _add_member(c, admin, a, "reader-sub", "reader")

    reader = _mint(rsa_key, sub="reader-sub", groups=[GROUP_READ])
    stranger = _mint(rsa_key, sub="nobody", groups=[GROUP_READ])

    seen = {p["slug"] for p in c.get("/api/v1/projects", headers=_auth(reader)).get_json()}
    assert seen == {a}
    all_seen = {p["slug"] for p in c.get("/api/v1/projects", headers=_auth(admin)).get_json()}
    assert {a, b} <= all_seen
    assert c.get("/api/v1/projects", headers=_auth(stranger)).get_json() == []


def test_on_projects_heads_is_isolation_scoped(app_on, rsa_key):
    """UI-DELTA-10 isolation: the batched ``GET /projects/heads`` fan-out is scoped
    to the SAME visible set as ``GET /projects``. A non-admin member sees only their
    project's head — a non-member's project head is ABSENT from the map (never a
    cross-project leak) — while a global spec-admin sees every project's head. Each
    present value equals that project's own ``/changes/head``."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    a = _mk_project(c, admin)
    b = _mk_project(c, admin)
    _add_member(c, admin, a, "reader-sub", "reader")
    # Mutate BOTH projects so both have a non-zero head to (not) leak.
    for slug in (a, b):
        assert c.post(f"/api/v1/projects/{slug}/tasks", json={"title": "T"},
                      headers=_auth(admin)).status_code == 201

    reader = _mint(rsa_key, sub="reader-sub", groups=[GROUP_READ])
    stranger = _mint(rsa_key, sub="nobody", groups=[GROUP_READ])

    # The reader member sees ONLY project a — b's head must be absent.
    reader_heads = c.get("/api/v1/projects/heads", headers=_auth(reader)).get_json()["heads"]
    assert set(reader_heads) == {a}
    assert b not in reader_heads
    # The present value matches a's own /changes/head (same verified caller).
    a_head = c.get(f"/api/v1/projects/{a}/changes/head", headers=_auth(reader)).get_json()
    assert reader_heads[a] == a_head

    # A member-of-nothing gets an empty map (fail-closed), never a leaked head.
    assert c.get("/api/v1/projects/heads", headers=_auth(stranger)).get_json()["heads"] == {}

    # A global spec-admin sees every project's head.
    admin_heads = c.get("/api/v1/projects/heads", headers=_auth(admin)).get_json()["heads"]
    assert {a, b} <= set(admin_heads)


def test_off_projects_heads_shows_all(app_off, rsa_key):
    """Flag OFF: the batched head map lists every project (membership ignored),
    mirroring ``GET /projects`` behaviour under the dormant flag."""
    c = app_off.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    a = _mk_project(c, admin)
    b = _mk_project(c, admin)
    reader = _mint(rsa_key, sub="stranger-r", groups=[GROUP_READ])
    heads = c.get("/api/v1/projects/heads", headers=_auth(reader)).get_json()["heads"]
    assert {a, b} <= set(heads)


def test_on_create_project_makes_creator_admin_member(app_on, rsa_key):
    """create_project records the VERIFIED creator as an ``admin`` member."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub=ADMIN_SUB, groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    members = c.get(f"/api/v1/projects/{slug}/members", headers=_auth(admin)).get_json()
    by_sub = {m["principal_sub"]: m for m in members}
    assert by_sub.get(ADMIN_SUB, {}).get("role") == "admin"


def test_on_duplicate_create_is_atomic_no_orphan_member(app_on, rsa_key):
    """Atomicity: a duplicate-slug create fails (409) and does NOT add a second
    creator-admin member — the member write is bound to the (failed) project
    write, so the project never exists with a stray extra admin."""
    c = app_on.test_client()
    admin_a = _mint(rsa_key, sub="creator-a", groups=[GROUP_ADMIN])
    admin_b = _mint(rsa_key, sub="creator-b", groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin_a)

    dup = c.post("/api/v1/projects", json={"slug": slug, "name": "P2"},
                 headers=_auth(admin_b))
    assert dup.status_code == 409, dup.get_json()

    members = c.get(f"/api/v1/projects/{slug}/members", headers=_auth(admin_a)).get_json()
    subs = {m["principal_sub"] for m in members}
    assert subs == {"creator-a"}  # creator-b's would-be member never landed
