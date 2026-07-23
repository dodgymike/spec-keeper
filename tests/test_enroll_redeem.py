"""Public agent-enrollment REDEEM endpoint tests (ONBOARD-3):
POST /api/v1/agent-enrollments/redeem.

The highest-risk surface: a PUBLIC, unauthenticated route that atomically BURNS a
single-use token and provisions a real Cognito credential. These tests prove:

  * valid token -> burns ONCE, provisions the Cognito user (spec-writers +
    project membership at the enrolled role), returns creds + recipe ONCE;
  * DOUBLE-submit the same token -> exactly ONE success; the second is the SAME
    generic failure and creates NO second Cognito user / NO second membership;
  * expired / unknown / already-used all fold into the SAME generic error (no
    enumeration oracle);
  * 501 when the enrollments table is unset, and when the pool is unset;
  * a re-minted token for the SAME agent_name is idempotent (tolerates an
    existing user, resets its password, still returns working creds);
  * the plaintext token and the generated password appear in NO log record.

The enrollments table + cognito-idp client are faked in-memory (monkeypatched
into ``app.blueprints.enroll``) so no DynamoDB Local / real Cognito is required;
the storage layer is the real Postgres test DB (membership is asserted for real).
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid

import pytest
from botocore.exceptions import ClientError

from app import create_app
from app.blueprints import enroll as enroll_bp
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _uname(app, agent_name: str, project_slug: str) -> str:
    """The project-namespaced provisioned username the endpoint will derive
    (ONBOARD-3a) — computed via the production helper so the tests track the
    scheme rather than hard-coding it."""
    return enroll_bp._provisioned_username(
        app.config, agent_name=agent_name, project_slug=project_slug
    )


# --------------------------------------------------------------------------- #
# Fakes                                                                         #
# --------------------------------------------------------------------------- #
class FakeEnrollTable:
    """In-memory enrollments Table enforcing the conditional single-use burn."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def seed(self, token: str, *, project_slug="demo", role="writer",
             agent_name="bot-1", status="active", ttl=3600):
        th = _sha(token)
        self.items[th] = {
            "token_hash": th, "project_slug": project_slug, "role": role,
            "agent_name": agent_name, "status": status,
            "expires_at": int(time.time()) + ttl,
        }
        return th

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ReturnValues=None):
        th = Key["token_hash"]
        it = self.items.get(th)
        now = int(ExpressionAttributeValues[":now"])
        # Mirror: attribute_exists(token_hash) AND status='active' AND expires_at>now
        ok = (
            it is not None
            and it.get("status") == "active"
            and int(it.get("expires_at", 0)) > now
        )
        if not ok:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        it["status"] = "used"
        it["used_at"] = now
        return {"Attributes": dict(it)}

    def get_item(self, Key):
        """Non-mutating point read (used by preview + the already-redeemed probe)."""
        it = self.items.get(Key["token_hash"])
        return {"Item": dict(it)} if it is not None else {}


class FakeCognito:
    """In-memory cognito-idp stand-in. Keyed by the email-alias Username."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.create_calls = 0
        self.setpw_calls = 0
        self.group_calls: list[tuple[str, str]] = []
        self.auth_calls: list[dict] = []
        self.auth_fails = False  # flip True to simulate a sign-in failure

    def initiate_auth(self, AuthFlow, ClientId, AuthParameters):
        """Server-side USER_PASSWORD_AUTH stand-in (ONBOARD-8). Verifies the flow +
        that the presented password matches what was set, then returns a fake
        AccessToken so tests need no real Cognito."""
        self.auth_calls.append({"flow": AuthFlow, "client_id": ClientId,
                                "username": AuthParameters.get("USERNAME")})
        if self.auth_fails:
            raise ClientError(
                {"Error": {"Code": "NotAuthorizedException"}}, "InitiateAuth"
            )
        assert AuthFlow == "USER_PASSWORD_AUTH"
        u = AuthParameters["USERNAME"]
        assert self.users[u]["password"] == AuthParameters["PASSWORD"]
        return {
            "AuthenticationResult": {
                "AccessToken": "access-" + u,
                "ExpiresIn": 3600,
                "RefreshToken": "refresh-" + u,
                "TokenType": "Bearer",
            }
        }

    def admin_create_user(self, UserPoolId, Username, MessageAction,
                          TemporaryPassword, UserAttributes):
        self.create_calls += 1
        if Username in self.users:
            raise ClientError(
                {"Error": {"Code": "UsernameExistsException"}}, "AdminCreateUser"
            )
        sub = "sub-" + uuid.uuid4().hex
        self.users[Username] = {"sub": sub, "password": TemporaryPassword, "groups": []}
        return {"User": {"Username": sub, "Attributes": [{"Name": "sub", "Value": sub}]}}

    def admin_get_user(self, UserPoolId, Username):
        u = self.users[Username]
        return {"Username": u["sub"], "UserAttributes": [{"Name": "sub", "Value": u["sub"]}]}

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.setpw_calls += 1
        self.users[Username]["password"] = Password

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.group_calls.append((Username, GroupName))
        self.users[Username]["groups"].append(GroupName)


@pytest.fixture
def fakes(monkeypatch):
    table = FakeEnrollTable()
    cog = FakeCognito()
    monkeypatch.setattr(enroll_bp, "_enrollments_table", lambda cfg: table)
    monkeypatch.setattr(enroll_bp, "_cognito_client", lambda cfg: cog)
    return table, cog


def _app(**overrides):
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        AGENT_ENROLLMENTS_TABLE = "spec-server-agent-enrollments"
        COGNITO_USER_POOL_ID = "eu-west-1_TESTPOOL"
        AWS_REGION = "eu-west-1"
        ENROLL_COGNITO_CLIENT_ID = "test-client-id"
        ENROLL_API_BASE = "https://api.example.com"
    for k, v in overrides.items():
        setattr(_Cfg, k, v)
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


def _mk_project(client, slug):
    r = client.post("/api/v1/projects", json={"slug": slug, "name": "P"})
    assert r.status_code == 201, r.get_json()
    return slug


# --------------------------------------------------------------------------- #
# Happy path                                                                    #
# --------------------------------------------------------------------------- #
def test_valid_token_burns_once_and_provisions(fakes):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    th = table.seed(token, project_slug=slug, role="writer", agent_name="alice-bot")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()

    # Working creds returned ONCE. The username is PROJECT-NAMESPACED (ONBOARD-3a):
    # the sanitized agent name, then the sanitized project slug, then a short hash.
    expected_user = _uname(app, "alice-bot", slug)
    assert body["username"] == expected_user
    assert body["username"].startswith("alice-bot.")
    assert body["username"].endswith("@agents.spec-server.internal")
    assert body["password"] and len(body["password"]) >= 16
    assert body["api_base"] == "https://api.example.com"
    assert body["region"] == "eu-west-1"
    assert body["client_id"] == "test-client-id"
    assert body["project_slug"] == slug
    assert body["role"] == "writer"
    # Recipe present with the three copy-paste steps.
    recipe = body["recipe"]
    assert set(recipe.keys()) == {"1_mint_token", "2_first_call", "3_migrate_local_backlog"}

    # Token burned exactly once (status flipped used).
    assert table.items[th]["status"] == "used"
    # Cognito user created once, in spec-writers ONLY (capability tier).
    assert cog.create_calls == 1
    assert cog.setpw_calls == 1
    assert cog.group_calls == [(expected_user, "spec-writers")]
    # Password on the created user matches the one returned (permanent set).
    assert cog.users[expected_user]["password"] == body["password"]

    # Membership granted at the enrolled role, keyed on the resolved sub.
    sub = cog.users[expected_user]["sub"]
    with app.app_context():
        m = app.storage.get_membership(slug, sub)
    assert m is not None and m.role == "writer"


def test_role_is_carried_from_the_burned_token(fakes):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="admin", agent_name="rolebot")
    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["role"] == "admin"
    expected_user = _uname(app, "rolebot", slug)
    # Capability tier is STILL spec-writers only — never spec-admins.
    assert cog.group_calls == [(expected_user, "spec-writers")]
    sub = cog.users[expected_user]["sub"]
    with app.app_context():
        assert app.storage.get_membership(slug, sub).role == "admin"


# --------------------------------------------------------------------------- #
# Strict single-use                                                             #
# --------------------------------------------------------------------------- #
def test_double_submit_exactly_one_success(fakes):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, agent_name="dupbot")

    r1 = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    r2 = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})

    assert r1.status_code == 201, r1.get_json()
    assert r2.status_code == 400, r2.get_json()
    # ONBOARD-8: a re-submit of a spent token gets the DISTINCT, actionable message
    # (not the generic one) — single-use is unchanged (the burn still failed).
    assert r2.get_json()["message"] == "This enrollment has already been redeemed."
    # No second Cognito user, no second provisioning.
    assert cog.create_calls == 1
    assert cog.setpw_calls == 1
    assert len(cog.users) == 1


# --------------------------------------------------------------------------- #
# Generic-error indistinguishability (no enumeration oracle)                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["unknown", "expired"])
def test_bad_tokens_all_same_generic_400(fakes, kind):
    """Missing / expired fold into the SAME generic 400 (no enumeration oracle).
    The already-USED case is the one exception — it earns a distinct message
    (asserted in test_used_token_says_already_redeemed)."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    token = "tok-" + uuid.uuid4().hex
    if kind == "expired":
        table.seed(token, ttl=-10)
    # "unknown" -> not seeded at all.

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["message"] == "invalid or expired enrollment token"
    # Never provisions on a bad token.
    assert cog.create_calls == 0


def test_used_token_says_already_redeemed(fakes):
    """ONBOARD-8: a token that was already consumed gets a DISTINCT, actionable
    400 message (vs the generic missing/expired one), via a NON-mutating probe —
    single-use is never weakened."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    token = "tok-" + uuid.uuid4().hex
    th = table.seed(token)
    table.items[th]["status"] = "used"

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["message"] == "This enrollment has already been redeemed."
    assert cog.create_calls == 0


# --------------------------------------------------------------------------- #
# 501 graceful degradation                                                      #
# --------------------------------------------------------------------------- #
def test_501_when_table_unset():
    # No fakes monkeypatch; AGENT_ENROLLMENTS_TABLE unset -> graceful 501.
    app = _app(AGENT_ENROLLMENTS_TABLE=None)
    r = app.test_client().post("/api/v1/agent-enrollments/redeem", json={"token": "x"})
    assert r.status_code == 501, r.get_json()


def test_501_when_pool_unset(monkeypatch):
    # Table configured (faked) but no Cognito pool -> graceful 501.
    table = FakeEnrollTable()
    monkeypatch.setattr(enroll_bp, "_enrollments_table", lambda cfg: table)
    app = _app(COGNITO_USER_POOL_ID=None)
    r = app.test_client().post("/api/v1/agent-enrollments/redeem", json={"token": "x"})
    assert r.status_code == 501, r.get_json()


def test_missing_token_is_422(fakes):
    app = _app()
    r = app.test_client().post("/api/v1/agent-enrollments/redeem", json={})
    assert r.status_code == 422, r.get_json()


def test_transient_backend_fault_is_503_not_400(fakes):
    """A NON-conditional DynamoDB fault during the burn leaves the token un-burned
    and must surface as a retryable 503 — never as "invalid token" (which would
    make the caller discard a still-valid token). Not an oracle: a genuinely bad
    token always yields ConditionalCheckFailed -> 400, so 503 == real fault."""
    table, cog = fakes
    app = _app()
    c = app.test_client()

    def _throttle(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )

    table.update_item = _throttle
    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": "tok-x"})
    assert r.status_code == 503, r.get_json()
    # Never provisions on a backend fault.
    assert cog.create_calls == 0


# --------------------------------------------------------------------------- #
# Idempotent provisioning for a re-minted token (same agent_name)               #
# --------------------------------------------------------------------------- #
def test_reminted_token_same_agent_is_idempotent(fakes):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)

    tok1 = "tok-" + uuid.uuid4().hex
    tok2 = "tok-" + uuid.uuid4().hex
    table.seed(tok1, project_slug=slug, agent_name="samebot")
    table.seed(tok2, project_slug=slug, agent_name="samebot")

    r1 = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok1})
    r2 = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok2})
    assert r1.status_code == 201, r1.get_json()
    assert r2.status_code == 201, r2.get_json()  # still working creds

    # Two create attempts, but only ONE Cognito user (2nd hit UsernameExists ->
    # password reset). Password set both times so the caller always gets creds.
    expected_user = _uname(app, "samebot", slug)
    assert cog.create_calls == 2
    assert len(cog.users) == 1
    assert cog.setpw_calls == 2
    # Both redeems targeted the SAME username (legitimate rotation of one user).
    assert r1.get_json()["username"] == expected_user == r2.get_json()["username"]
    assert r2.get_json()["password"] == cog.users[expected_user]["password"]


# --------------------------------------------------------------------------- #
# ONBOARD-3a — cross-tenant identity isolation                                  #
# --------------------------------------------------------------------------- #
def test_same_agent_name_different_projects_are_distinct_users(fakes):
    """The heart of ONBOARD-3a: two enrollments with the SAME agent_name for
    DIFFERENT projects provision TWO DISTINCT Cognito users. Neither redeem resets
    the other's password, and each sub is a member of ONLY its own project (no
    cross-tenant membership escalation)."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug_a = f"proj-{uuid.uuid4().hex[:8]}"
    slug_b = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug_a)
    _mk_project(c, slug_b)
    tok_a = "tok-" + uuid.uuid4().hex
    tok_b = "tok-" + uuid.uuid4().hex
    table.seed(tok_a, project_slug=slug_a, role="writer", agent_name="dup")
    table.seed(tok_b, project_slug=slug_b, role="writer", agent_name="dup")

    ra = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok_a})
    rb = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok_b})
    assert ra.status_code == 201, ra.get_json()
    assert rb.status_code == 201, rb.get_json()
    ua, ub = ra.get_json()["username"], rb.get_json()["username"]

    # DISTINCT usernames despite identical agent_name -> two separate users, each
    # created once, each password set once (NEITHER reset via the other).
    assert ua != ub
    assert cog.create_calls == 2
    assert len(cog.users) == 2
    assert cog.setpw_calls == 2
    assert cog.users[ua]["password"] == ra.get_json()["password"]
    assert cog.users[ub]["password"] == rb.get_json()["password"]

    # Each sub is a member of ONLY its own project — no cross-membership.
    sub_a, sub_b = cog.users[ua]["sub"], cog.users[ub]["sub"]
    with app.app_context():
        assert app.storage.get_membership(slug_a, sub_a) is not None
        assert app.storage.get_membership(slug_b, sub_a) is None
        assert app.storage.get_membership(slug_b, sub_b) is not None
        assert app.storage.get_membership(slug_a, sub_b) is None


def test_reredeem_same_project_agent_targets_same_username(fakes):
    """Re-redeeming the SAME (project, agent_name) targets the SAME Cognito user —
    a legitimate password rotation, not a new identity."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    tok1 = "tok-" + uuid.uuid4().hex
    tok2 = "tok-" + uuid.uuid4().hex
    table.seed(tok1, project_slug=slug, agent_name="rot")
    table.seed(tok2, project_slug=slug, agent_name="rot")

    r1 = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok1})
    r2 = c.post("/api/v1/agent-enrollments/redeem", json={"token": tok2})
    assert r1.status_code == 201, r1.get_json()
    assert r2.status_code == 201, r2.get_json()
    assert r1.get_json()["username"] == r2.get_json()["username"]  # SAME user
    assert len(cog.users) == 1


# --------------------------------------------------------------------------- #
# Provisioning failure after burn -> 500, token stays spent                     #
# --------------------------------------------------------------------------- #
def test_provision_failure_after_burn_is_500_and_token_spent(fakes):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    th = table.seed(token, project_slug=slug, agent_name="failbot")

    def _boom(**kwargs):
        raise ClientError({"Error": {"Code": "InternalErrorException"}}, "AdminCreateUser")

    cog.admin_create_user = _boom
    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 500, r.get_json()
    # Token stays burned (single-use preserved; remedy is a fresh enrollment).
    assert table.items[th]["status"] == "used"


# --------------------------------------------------------------------------- #
# Secrets never logged                                                          #
# --------------------------------------------------------------------------- #
def test_token_and_password_never_logged(fakes, caplog):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, agent_name="quietbot")

    with caplog.at_level(logging.DEBUG):
        r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    password = r.get_json()["password"]

    for rec in caplog.records:
        msg = rec.getMessage()
        assert token not in msg
        assert password not in msg


def test_token_and_password_never_logged_on_rejection(fakes, caplog):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    token = "tok-" + uuid.uuid4().hex  # unknown -> rejected
    with caplog.at_level(logging.DEBUG):
        r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 400
    for rec in caplog.records:
        assert token not in rec.getMessage()


# --------------------------------------------------------------------------- #
# ONBOARD-8 — headless-usable redeem: server-side sign-in + ready import        #
# --------------------------------------------------------------------------- #
def test_redeem_returns_access_token_and_import_curl(fakes):
    """The server signs in on the agent's behalf and hands back a ready Bearer
    AccessToken plus a copy-paste import_curl carrying the REAL import URL + that
    bearer — a headless agent can paste-and-run with zero Cognito round-trip."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="writer", agent_name="curlbot")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()

    # The credential-bearing response is marked no-store (defense-in-depth).
    assert r.headers.get("Cache-Control") == "no-store"

    expected_user = _uname(app, "curlbot", slug)
    # Ready Bearer credential — the AccessToken (correct token_use), never empty.
    assert body["access_token"] == "access-" + expected_user
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["refresh_token"] == "refresh-" + expected_user
    assert body["note"] is None
    # Server actually ran USER_PASSWORD_AUTH against the agents client id.
    assert cog.auth_calls == [
        {"flow": "USER_PASSWORD_AUTH", "client_id": "test-client-id",
         "username": expected_user}
    ]
    # import_url + a literal, ready-to-run curl with the real bearer substituted.
    import_url = f"https://api.example.com/api/v1/projects/{slug}/import"
    assert body["import_url"] == import_url
    curl = body["import_curl"]
    assert import_url in curl
    assert f"Authorization: Bearer {body['access_token']}" in curl
    assert "Content-Type: text/markdown" in curl
    assert "User-Agent: spec-agent/1.0" in curl
    assert "--data-binary @SPEC.md" in curl
    # Ordered next steps present and non-empty.
    assert isinstance(body["next"], list) and len(body["next"]) >= 2


def test_redeem_next_steps_instruct_persist_for_reauth(fakes):
    """ONBOARD-11: the redeem response must tell a headless agent, EARLY and
    prominently, to PERSIST its one-time Cognito credentials for later re-auth —
    the enrollment link is single-use and will never show them again. The first
    'next' step carries the save/single-use warning + the re-auth recipe
    (USER_PASSWORD_AUTH / refresh), and it is mirrored in the recipe. Neither the
    password nor the access_token leaks into that instruction text."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="writer", agent_name="persistbot")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()

    steps = body["next"]
    assert isinstance(steps, list) and steps
    # The PERSIST instruction is the FIRST (earliest) step.
    persist = steps[0]
    low = persist.lower()
    assert "save" in low
    assert "single-use" in low or "single use" in low
    # It names HOW to re-auth without re-enrolling.
    assert "USER_PASSWORD_AUTH" in persist or "REFRESH_TOKEN_AUTH" in persist
    assert "re-auth" in low or "re-enroll" in low or "expires" in low
    # And it uses the non-secret client_id/region already in the response.
    assert body["client_id"] in persist
    assert body["region"] in persist
    # Mirrored into the recipe too.
    recipe_text = " ".join(body["recipe"].values()).lower()
    assert "single-use" in recipe_text and "save" in recipe_text

    # The instruction text NEVER embeds the live secrets.
    assert body["password"] not in persist
    assert body["access_token"] not in persist
    for v in body["recipe"].values():
        assert body["password"] not in v
        assert body["access_token"] not in v


def test_redeem_fallback_note_warns_single_use_persist(fakes):
    """ONBOARD-11: when server-side sign-in falls back (no access_token), the note
    must ALSO warn the creds are single-use and instruct saving + re-auth."""
    table, cog = fakes
    cog.auth_fails = True
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="writer", agent_name="notebot")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    note = r.get_json()["note"]
    low = note.lower()
    assert "save" in low
    assert "single-use" in low or "single use" in low
    assert "USER_PASSWORD_AUTH" in note or "REFRESH_TOKEN_AUTH" in note


def test_redeem_falls_back_when_signin_fails(fakes):
    """If server-side initiate_auth fails, redeem STILL succeeds (single-use burn
    already happened) — access_token is null, a clear note is returned, and the raw
    creds let the agent self-auth. import_curl leaves a placeholder bearer."""
    table, cog = fakes
    cog.auth_fails = True
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="writer", agent_name="fallbot")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["access_token"] is None
    assert body["note"] and "AccessToken" in body["note"]
    # Raw creds still usable for self-auth.
    assert body["username"] and body["password"]
    assert body["client_id"] == "test-client-id"
    # Placeholder bearer in the curl (agent substitutes after self-auth).
    assert "<access_token>" in body["import_curl"]


def test_redeem_signin_fallback_on_malformed_response(fakes):
    """ONBOARD-8 F1: a MALFORMED sign-in success shape (e.g. a non-numeric
    ExpiresIn that trips int()) must degrade to the raw-creds fallback — never a
    500 that discards the already-burned token + provisioned agent."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    th = table.seed(token, project_slug=slug, agent_name="malfbot")

    def _malformed(**kwargs):
        cog.auth_calls.append({"flow": kwargs.get("AuthFlow"),
                               "client_id": kwargs.get("ClientId"),
                               "username": kwargs.get("AuthParameters", {}).get("USERNAME")})
        return {"AuthenticationResult": {"AccessToken": "x", "ExpiresIn": "not-an-int"}}

    cog.initiate_auth = _malformed
    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()  # NEVER a 500
    body = r.get_json()
    assert body["access_token"] is None
    assert body["note"]
    # Token stays burned + agent provisioned (recovery is a fresh enrollment).
    assert table.items[th]["status"] == "used"


def test_redeem_signin_fallback_when_client_id_unset(fakes):
    """No ENROLL_COGNITO_CLIENT_ID -> the server cannot sign in; falls back to raw
    creds + note WITHOUT ever calling initiate_auth."""
    table, cog = fakes
    app = _app(ENROLL_COGNITO_CLIENT_ID=None)
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, agent_name="noclient")

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["access_token"] is None
    assert cog.auth_calls == []  # never attempted without a client id


def test_access_token_and_password_never_logged(fakes, caplog):
    """The emitted access_token + password appear in NO log record (ONBOARD-8)."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, agent_name="tokenquiet")

    with caplog.at_level(logging.DEBUG):
        r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    for rec in caplog.records:
        msg = rec.getMessage()
        assert body["access_token"] not in msg
        assert body["password"] not in msg


# --------------------------------------------------------------------------- #
# ONBOARD-8 — non-consuming PREVIEW                                             #
# --------------------------------------------------------------------------- #
def test_preview_returns_project_role_without_burning(fakes):
    """Preview reveals project/role/agent/expiry for an active token and does NOT
    burn it — the token is still redeemable afterwards."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    th = table.seed(token, project_slug=slug, role="writer", agent_name="peekbot")

    r = c.post("/api/v1/agent-enrollments/preview", json={"token": token})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["valid"] is True
    assert body["project_slug"] == slug
    assert body["role"] == "writer"
    assert body["agent_name"] == "peekbot"
    assert isinstance(body["expires_at"], int)
    # NOT burned: still active, and no Cognito user provisioned.
    assert table.items[th]["status"] == "active"
    assert cog.create_calls == 0


def test_preview_then_redeem_works(fakes):
    """Inspect-before-commit: preview then a real redeem of the SAME token."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    slug = f"proj-{uuid.uuid4().hex[:8]}"
    _mk_project(c, slug)
    token = "tok-" + uuid.uuid4().hex
    table.seed(token, project_slug=slug, role="writer", agent_name="twostep")

    p = c.post("/api/v1/agent-enrollments/preview", json={"token": token})
    assert p.status_code == 200 and p.get_json()["valid"] is True

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["access_token"]


@pytest.mark.parametrize("kind", ["unknown", "expired", "used"])
def test_preview_generic_false_for_bad_tokens(fakes, kind):
    """Missing / expired / used all fold into the SAME generic {valid:false}
    (no enumeration oracle) — and preview never burns."""
    table, cog = fakes
    app = _app()
    c = app.test_client()
    token = "tok-" + uuid.uuid4().hex
    if kind == "expired":
        table.seed(token, ttl=-10)
    elif kind == "used":
        th = table.seed(token)
        table.items[th]["status"] = "used"

    r = c.post("/api/v1/agent-enrollments/preview", json={"token": token})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["valid"] is False
    # No project/role leaked on a non-valid token.
    assert not body.get("project_slug")
    assert not body.get("role")


def test_preview_501_when_table_unset():
    app = _app(AGENT_ENROLLMENTS_TABLE=None)
    r = app.test_client().post("/api/v1/agent-enrollments/preview", json={"token": "x"})
    assert r.status_code == 501, r.get_json()


# --------------------------------------------------------------------------- #
# ONBOARD-8 — machine-readable DISCOVERY                                        #
# --------------------------------------------------------------------------- #
def test_discovery_returns_protocol_json(fakes):
    app = _app()
    r = app.test_client().get("/api/v1/agent-enrollments")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["preview_url"].endswith("/api/v1/agent-enrollments/preview")
    assert body["redeem_url"].endswith("/api/v1/agent-enrollments/redeem")
    assert body["request_body"] == {"token": "the value after #token= in your enrollment URL"}
    assert "#token=" in body["token_source"]
    assert "Bearer" in body["authorization"]
    assert isinstance(body["steps"], list) and body["steps"]


def test_discovery_needs_no_token(fakes):
    """Discovery is a plain GET with no body/token required."""
    app = _app()
    r = app.test_client().get("/api/v1/agent-enrollments")
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# ONBOARD-8 — 429 carries Retry-After                                           #
# --------------------------------------------------------------------------- #
def test_rate_limited_redeem_carries_retry_after(fakes, monkeypatch):
    monkeypatch.setattr(enroll_bp, "rate_limited", lambda *a, **k: True)
    app = _app(SIGNUP_RATELIMIT_WINDOW_S=42)
    c = app.test_client()
    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": "tok-x"})
    assert r.status_code == 429, r.get_json()
    assert r.headers.get("Retry-After") == "42"


def test_rate_limited_preview_carries_retry_after(fakes, monkeypatch):
    monkeypatch.setattr(enroll_bp, "rate_limited", lambda *a, **k: True)
    app = _app(SIGNUP_RATELIMIT_WINDOW_S=42)
    c = app.test_client()
    r = c.post("/api/v1/agent-enrollments/preview", json={"token": "tok-x"})
    assert r.status_code == 429, r.get_json()
    assert r.headers.get("Retry-After") == "42"


def test_rate_limited_discovery_carries_retry_after(fakes, monkeypatch):
    monkeypatch.setattr(enroll_bp, "rate_limited", lambda *a, **k: True)
    app = _app(SIGNUP_RATELIMIT_WINDOW_S=42)
    c = app.test_client()
    r = c.get("/api/v1/agent-enrollments")
    assert r.status_code == 429, r.get_json()
    assert r.headers.get("Retry-After") == "42"
