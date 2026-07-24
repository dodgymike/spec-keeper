"""SEC-FIX-1: JIRA config cross-tenant IDOR + SSRF remediation.

Three vulnerabilities, one file:

* **Fix 1 (P0) — project-isolation bypass.** The jira-config GET/POST/PUT handlers
  used the bare global-group gate (``require_api_key``) instead of the per-project
  gate. With ``PROJECT_ISOLATION_ENFORCED`` on, any enrolled agent could read or
  overwrite ANOTHER project's Jira config. Proven denied on BOTH storage backends
  via the Cognito-JWT isolation harness (a non-member A-agent is 404 on GET and 403
  on POST/PUT against project B; a genuine B-member with write succeeds).

* **Fix 2 (P1) — SSRF via unvalidated ``base_url``.** The server calls ``base_url``
  server-side, so it is validated at the schema boundary (422) AND re-validated in
  ``JiraClient`` (defense-in-depth): https-only, no private/loopback/link-local IP
  literals, no ``localhost``, host on the ``.atlassian.net`` allow-list.

* **Fix 3 (P2) — bounded persisted error text.** ``jira_sync_error`` is reader-visible,
  so a failed sync persists only a generic ``sync failed (HTTP <code>)`` — never the
  raw upstream body (covered here with a long body + in ``test_jira_sync``).
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import helpers
from app.jira_client import JiraClient, JiraClientError
from app.jira_url import JiraUrlError, validate_jira_base_url
from app.jira_sync import _safe_sync_error

# Reuse the ISO-4 Cognito-JWT harness building blocks (functions carry their own
# module globals: ISSUER / KID / AUDIENCE / _build_app / _mint all stay consistent).
from tests.test_isolation import (
    GROUP_ADMIN,
    GROUP_WRITE,
    KID,
    _BACKENDS,
    _add_member,
    _auth,
    _build_app,
    _mint,
    _mk_project,
)


# --------------------------------------------------------------------------- #
# Fix 2 (P1) — SSRF: pure-unit validation of the shared guard + JiraClient.
# --------------------------------------------------------------------------- #
REJECTED_URLS = [
    "http://foo.atlassian.net",          # non-https
    "https://169.254.169.254/rest",      # link-local (cloud metadata)
    "https://localhost",                 # localhost by name
    "https://10.0.0.5",                  # private IPv4
    "https://127.0.0.1",                 # loopback
    "https://192.168.1.10",              # private IPv4
    "https://[::1]/rest",                # IPv6 loopback
    "https://0.0.0.0",                   # unspecified
    "https://evil.example.com",          # non-allow-listed host
    "https://user:pass@foo.atlassian.net",  # embedded credentials
    "https://foo.atlassian.net:8443",    # non-default port
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_validate_jira_base_url_rejects(url):
    with pytest.raises(JiraUrlError):
        validate_jira_base_url(url)


def test_validate_jira_base_url_accepts_atlassian():
    assert validate_jira_base_url("https://foo.atlassian.net") == "https://foo.atlassian.net"


def test_validate_jira_base_url_honours_custom_allow_list():
    # A self-hosted host is permitted only when explicitly added to the allow-list.
    ok = validate_jira_base_url("https://jira.mycorp.com", [".mycorp.com"])
    assert ok == "https://jira.mycorp.com"
    with pytest.raises(JiraUrlError):
        validate_jira_base_url("https://jira.mycorp.com")  # default list rejects


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_jiraclient_defense_in_depth_rejects(url):
    """A value that somehow bypassed the schema still can't reach an internal host:
    JiraClient.__init__ re-validates and raises before any session is used."""
    with pytest.raises(JiraUrlError):
        JiraClient(base_url=url, email="a@b.com", api_token="tok")


def test_jiraclient_sets_request_timeout():
    """Every JiraClient request carries an explicit timeout (no hang on SSRF target)."""
    from unittest.mock import MagicMock, patch

    client = JiraClient(base_url="https://foo.atlassian.net", email="a@b.com", api_token="t")
    mock_resp = MagicMock(ok=True)
    mock_resp.json.return_value = {"key": "PROJ-1"}
    with patch.object(client._session, "request", return_value=mock_resp) as mock_req:
        client.create_issue("PROJ", "s", "d")
    _, kwargs = mock_req.call_args
    assert kwargs.get("timeout") == JiraClient.DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# Fix 3 (P2) — bounded persisted error text (no upstream body leak).
# --------------------------------------------------------------------------- #
def test_safe_sync_error_bounds_jira_client_error():
    long_body = "SENSITIVE-UPSTREAM-BODY " * 100
    exc = JiraClientError(500, long_body, "POST", "https://foo.atlassian.net/rest/api/3/issue")
    msg = _safe_sync_error(exc)
    assert msg == "sync failed (HTTP 500)"
    assert "SENSITIVE-UPSTREAM-BODY" not in msg


def test_safe_sync_error_generic_for_other_exceptions():
    assert _safe_sync_error(ValueError("secret internal detail")) == "sync failed"
    assert "secret internal detail" not in _safe_sync_error(ValueError("secret internal detail"))


# --------------------------------------------------------------------------- #
# Fix 1 (P0) — cross-tenant IDOR, proven on BOTH backends via the JWT harness.
# --------------------------------------------------------------------------- #
_VALID_CFG = {
    "base_url": "https://tenant.atlassian.net",
    "email": "agent@tenant.com",
    "api_token": "super-secret-token",
    "jira_project_key": "PROJ",
    "enabled": False,  # keep the path network-free (no warmup call)
}


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
def _encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture(params=_BACKENDS)
def app_on(request):
    app = _build_app(request.param, enforced=True)
    yield app
    app._teardown()


def test_cross_tenant_jira_config_denied_on_both_backends(app_on, rsa_key):
    """A member of project A (not B) is DENIED B's jira-config; a genuine B-member
    with write succeeds. Read denial is hidden (404); write denial is 403."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub="admin-sub", groups=[GROUP_ADMIN])
    proj_a = _mk_project(c, admin)
    proj_b = _mk_project(c, admin)

    # alice writes on A only; bob writes on B.
    _add_member(c, admin, proj_a, "alice-sub", "writer")
    _add_member(c, admin, proj_b, "bob-sub", "writer")
    alice = _mint(rsa_key, sub="alice-sub", groups=[GROUP_WRITE])
    bob = _mint(rsa_key, sub="bob-sub", groups=[GROUP_WRITE])

    base_b = f"/api/v1/projects/{proj_b}/jira-config"

    # --- IDOR denied: alice (member of A, not B) cannot touch B's config -------
    # GET: existence hidden -> 404 (byte-identical to a missing project).
    assert c.get(base_b, headers=_auth(alice)).status_code == 404
    # POST/PUT: project exists but access denied -> 403 (NOT 201/200).
    assert c.post(base_b, json=_VALID_CFG, headers=_auth(alice)).status_code == 403
    assert c.put(base_b, json={"base_url": "https://evil.atlassian.net"},
                 headers=_auth(alice)).status_code == 403

    # The attacker's write NEVER landed: B still has no config (bob, a real member,
    # gets 404 on GET before he creates one).
    assert c.get(base_b, headers=_auth(bob)).status_code == 404

    # --- Legitimate B-member with write succeeds ------------------------------
    created = c.post(base_b, json=_VALID_CFG, headers=_auth(bob))
    assert created.status_code == 201, created.get_json()
    assert c.get(base_b, headers=_auth(bob)).status_code == 200
    upd = c.put(base_b, json={"base_url": "https://tenant2.atlassian.net"},
                headers=_auth(bob))
    assert upd.status_code == 200, upd.get_json()
    assert upd.get_json()["base_url"] == "https://tenant2.atlassian.net"


def test_ssrf_rejected_over_http_on_both_backends(app_on, rsa_key):
    """A member with write cannot point base_url at an internal/SSRF target — the
    schema rejects it 422 (proven through the real HTTP stack, both backends), while
    a valid Jira Cloud host is accepted."""
    c = app_on.test_client()
    admin = _mint(rsa_key, sub="admin-sub", groups=[GROUP_ADMIN])
    slug = _mk_project(c, admin)
    _add_member(c, admin, slug, "wr-sub", "writer")
    writer = _mint(rsa_key, sub="wr-sub", groups=[GROUP_WRITE])
    base = f"/api/v1/projects/{slug}/jira-config"

    for bad in ("http://tenant.atlassian.net", "https://169.254.169.254",
                "https://localhost", "https://10.0.0.5", "https://evil.example.com"):
        body = dict(_VALID_CFG, base_url=bad)
        r = c.post(base, json=body, headers=_auth(writer))
        assert r.status_code == 422, (bad, r.get_json())

    # A valid Jira Cloud host is accepted.
    ok = c.post(base, json=_VALID_CFG, headers=_auth(writer))
    assert ok.status_code == 201, ok.get_json()
