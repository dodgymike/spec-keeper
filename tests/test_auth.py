"""AUTH-2 / AUTH-7 / AUTH-10: Cognito JWT validation, GROUP-based authz,
precedence, CORS.

These tests exercise the JWT path WITHOUT real Cognito: we mint an RSA keypair
in-process, serve a JWKS built from it by monkeypatching the JWKS fetch, and
sign access tokens carrying a ``cognito:groups`` claim (valid / expired /
wrong-aud / wrong-iss / no-groups / HS256 / alg=none). Authorization is by group
membership (spec-admins => read+write+admin, spec-writers => read+write,
spec-readers => read). They build their own app instances so the session-scoped
auth-off app (and the baseline suite) is untouched.
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

ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_TESTPOOL"
JWKS_URI = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "test-agents-client-id"
KID = "test-key-1"

# Cognito groups delivered in the access token's cognito:groups claim.
GROUP_READ = "spec-readers"
GROUP_WRITE = "spec-writers"
GROUP_ADMIN = "spec-admins"


# --------------------------------------------------------------------------- #
# Key material + JWKS
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    # Build a public JWK (dict) from the private key's public half.
    algo = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    public_jwk = algo.to_jwk(rsa_key.public_key(), as_dict=True)
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, jwks):
    """Serve our in-memory JWKS and start each test with a cold cache."""
    calls = {"n": 0}

    def fake_fetch(uri):
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(helpers, "_http_get_json", fake_fetch)
    helpers._reset_jwks_cache()
    yield calls
    helpers._reset_jwks_cache()


def _mint(rsa_key, *, groups=(GROUP_READ,), aud=AUDIENCE, iss=ISSUER,
          exp_delta=3600, token_use="access", alg="RS256", kid=KID):
    """Mint an access token. ``groups`` becomes the cognito:groups claim; pass
    ``None`` to omit the claim entirely (a token with no group membership)."""
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": "agent-sub",
        "client_id": aud,
        "token_use": token_use,
        "iat": now,
        "nbf": now - 1,
        "exp": now + exp_delta,
    }
    if groups is not None:
        claims["cognito:groups"] = list(groups)
    if aud is not None:
        claims["aud"] = aud
    headers = {"kid": kid}
    key = rsa_key if alg == "RS256" else "shared-hmac-secret-32-bytes-long-xxxxx"
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


# --------------------------------------------------------------------------- #
# App factories for each precedence rung
# --------------------------------------------------------------------------- #
def _make_app(**overrides):
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
    for k, v in overrides.items():
        setattr(_Cfg, k, v)
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def cognito_app():
    return _make_app(
        COGNITO_ISSUER=ISSUER,
        COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE],
        # Pin the clock-skew leeway to 0 so the expiry/nbf matrix is deterministic
        # and independent of the shipped AUTH_LEEWAY default (45s, SEC-FIX-4); a
        # token minted with exp_delta=-10 must read as genuinely expired here.
        AUTH_LEEWAY=0,
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _uslug(prefix: str = "p") -> str:
    """A unique project slug so these tests stay idempotent against a DB that the
    per-test ``_make_app`` helper never drops (it only create_all's)."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# JWT verification matrix
# --------------------------------------------------------------------------- #
def test_valid_read_token_allows_get(cognito_app, rsa_key):
    c = cognito_app.test_client()
    r = c.get("/api/v1/projects", headers=_auth(_mint(rsa_key, groups=[GROUP_READ])))
    assert r.status_code == 200, r.get_json()


def test_missing_token_is_401(cognito_app):
    r = cognito_app.test_client().get("/api/v1/projects")
    assert r.status_code == 401
    body = r.get_json()
    assert set(body) == {"code", "status", "message"}
    assert body["code"] == 401


def test_expired_token_is_401(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_READ], exp_delta=-10)
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_wrong_audience_is_401(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_READ], aud="some-other-client")
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_wrong_issuer_is_401(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_READ], iss="https://evil.example.com/pool")
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_wrong_token_use_is_401(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_READ], token_use="id")
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_unknown_kid_is_401(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_READ], kid="not-a-real-kid")
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_hs256_alg_confusion_is_rejected(cognito_app, rsa_key):
    """A token signed HS256 must not be accepted by the RS256-pinned verifier."""
    tok = _mint(rsa_key, groups=[GROUP_READ], alg="HS256")
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


def test_alg_none_is_rejected(cognito_app, rsa_key):
    now = int(time.time())
    tok = jwt.encode(
        {"iss": ISSUER, "cognito:groups": [GROUP_ADMIN], "token_use": "access",
         "iat": now, "exp": now + 3600, "aud": AUDIENCE},
        key=None, algorithm="none", headers={"kid": KID},
    )
    r = cognito_app.test_client().get("/api/v1/projects", headers=_auth(tok))
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Group enforcement (cognito:groups -> permission -> method + resource)
# --------------------------------------------------------------------------- #
def test_reader_can_get_but_not_mutate(cognito_app, rsa_key):
    c = cognito_app.test_client()
    tok = _mint(rsa_key, groups=[GROUP_READ])
    assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 200
    # A reader lacks write/admin -> any mutation is 403.
    r = c.post("/api/v1/projects", json={"slug": "x", "name": "X"}, headers=_auth(tok))
    assert r.status_code == 403
    assert r.get_json()["code"] == 403


def test_admin_group_allows_project_create(cognito_app, rsa_key):
    tok = _mint(rsa_key, groups=[GROUP_ADMIN])
    r = cognito_app.test_client().post(
        "/api/v1/projects", json={"slug": _uslug("authproj"), "name": "Auth"},
        headers=_auth(tok),
    )
    assert r.status_code == 201, r.get_json()


def test_writer_creates_task_and_reads_but_cannot_admin(cognito_app, rsa_key):
    c = cognito_app.test_client()
    slug = _uslug("wproj")
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    assert c.post("/api/v1/projects", json={"slug": slug, "name": "W"},
                  headers=_auth(admin)).status_code == 201
    write = _mint(rsa_key, groups=[GROUP_WRITE])
    # spec-writers => {read, write}: can GET and can create a task...
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(write)).status_code == 200
    r = c.post(f"/api/v1/projects/{slug}/tasks",
               json={"key": "T-1", "title": "task"}, headers=_auth(write))
    assert r.status_code == 201, r.get_json()
    # ...but must NOT create a project (needs admin).
    r2 = c.post("/api/v1/projects", json={"slug": _uslug("nope"), "name": "N"},
                headers=_auth(write))
    assert r2.status_code == 403


def test_multiple_groups_union_their_permissions(cognito_app, rsa_key):
    """A token in reader + writer groups gets the union (read+write)."""
    c = cognito_app.test_client()
    slug = _uslug("uproj")
    admin = _mint(rsa_key, groups=[GROUP_ADMIN])
    assert c.post("/api/v1/projects", json={"slug": slug, "name": "U"},
                  headers=_auth(admin)).status_code == 201
    tok = _mint(rsa_key, groups=[GROUP_READ, GROUP_WRITE])
    assert c.get(f"/api/v1/projects/{slug}/tasks", headers=_auth(tok)).status_code == 200
    assert c.post(f"/api/v1/projects/{slug}/tasks",
                  json={"key": "U-1", "title": "t"}, headers=_auth(tok)).status_code == 201


def test_no_groups_is_403_on_read(cognito_app, rsa_key):
    """A valid token carrying no cognito:groups grants no permissions -> 403."""
    c = cognito_app.test_client()
    tok = _mint(rsa_key, groups=None)  # omit the claim entirely
    assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 403
    # An unknown/empty group also yields no permissions.
    tok2 = _mint(rsa_key, groups=["some-other-pool-group"])
    assert c.get("/api/v1/projects", headers=_auth(tok2)).status_code == 403


# --------------------------------------------------------------------------- #
# JWKS caching: repeated requests must not re-fetch
# --------------------------------------------------------------------------- #
def test_jwks_is_cached_across_requests(cognito_app, rsa_key, _patch_jwks):
    c = cognito_app.test_client()
    for _ in range(5):
        assert c.get("/api/v1/projects",
                     headers=_auth(_mint(rsa_key, groups=[GROUP_READ]))).status_code == 200
    assert _patch_jwks["n"] == 1  # fetched exactly once, then served from cache


def test_unknown_kid_flood_does_not_amplify_fetches(cognito_app, rsa_key, _patch_jwks):
    """A burst of tokens with novel bogus kids must not become a burst of
    outbound JWKS fetches (DoS amplification guard). The default 30s cooldown
    keeps it to a single fetch."""
    c = cognito_app.test_client()
    for i in range(6):
        tok = _mint(rsa_key, groups=[GROUP_READ], kid=f"bogus-kid-{i}")
        assert c.get("/api/v1/projects", headers=_auth(tok)).status_code == 401
    assert _patch_jwks["n"] == 1


# --------------------------------------------------------------------------- #
# Precedence ladder
# --------------------------------------------------------------------------- #
def test_precedence_open_when_nothing_configured(rsa_key):
    app = _make_app()  # no cognito, no api keys
    r = app.test_client().get("/api/v1/projects")
    assert r.status_code == 200


def test_precedence_api_keys_when_no_cognito():
    app = _make_app(API_KEYS=["secret-key"])
    c = app.test_client()
    assert c.get("/api/v1/projects").status_code == 401
    assert c.get("/api/v1/projects",
                 headers={"Authorization": "Bearer secret-key"}).status_code == 200


def test_precedence_cognito_wins_over_api_keys(cognito_app, rsa_key):
    """When both are set, a static API key must NOT be accepted."""
    app = _make_app(
        COGNITO_ISSUER=ISSUER, COGNITO_JWKS_URI=JWKS_URI,
        COGNITO_AUDIENCE=[AUDIENCE], API_KEYS=["secret-key"],
    )
    c = app.test_client()
    assert c.get("/api/v1/projects",
                 headers={"Authorization": "Bearer secret-key"}).status_code == 401
    assert c.get("/api/v1/projects",
                 headers=_auth(_mint(rsa_key, groups=[GROUP_READ]))).status_code == 200


# --------------------------------------------------------------------------- #
# CORS (AUTH-7)
# --------------------------------------------------------------------------- #
def test_cors_preflight_echoes_allowlisted_origin_not_wildcard():
    app = _make_app(CORS_ORIGINS=["https://dash.example.com"])
    r = app.test_client().open(
        "/api/v1/projects", method="OPTIONS",
        headers={"Origin": "https://dash.example.com",
                 "Access-Control-Request-Method": "GET"},
    )
    aco = r.headers.get("Access-Control-Allow-Origin")
    assert aco == "https://dash.example.com"
    assert aco != "*"
    assert r.headers.get("Access-Control-Allow-Credentials") == "true"
    assert "Authorization" in r.headers.get("Access-Control-Allow-Headers", "")


def test_cors_disallowed_origin_gets_no_header():
    app = _make_app(CORS_ORIGINS=["https://dash.example.com"])
    r = app.test_client().open(
        "/api/v1/projects", method="OPTIONS",
        headers={"Origin": "https://evil.example.com",
                 "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("Access-Control-Allow-Origin") is None


def test_cors_disabled_by_default_no_headers():
    app = _make_app()
    r = app.test_client().get(
        "/api/v1/projects", headers={"Origin": "https://dash.example.com"}
    )
    assert r.headers.get("Access-Control-Allow-Origin") is None
