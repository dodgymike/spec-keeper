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


class FakeCognito:
    """In-memory cognito-idp stand-in. Keyed by the email-alias Username."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.create_calls = 0
        self.setpw_calls = 0
        self.group_calls: list[tuple[str, str]] = []

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

    # Working creds returned ONCE.
    assert body["username"] == "alice-bot@agents.spec-server.internal"
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
    assert cog.group_calls == [("alice-bot@agents.spec-server.internal", "spec-writers")]
    # Password on the created user matches the one returned (permanent set).
    assert cog.users["alice-bot@agents.spec-server.internal"]["password"] == body["password"]

    # Membership granted at the enrolled role, keyed on the resolved sub.
    sub = cog.users["alice-bot@agents.spec-server.internal"]["sub"]
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
    # Capability tier is STILL spec-writers only — never spec-admins.
    assert cog.group_calls == [("rolebot@agents.spec-server.internal", "spec-writers")]
    sub = cog.users["rolebot@agents.spec-server.internal"]["sub"]
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
    assert r2.get_json()["message"] == "invalid or expired enrollment token"
    # No second Cognito user, no second provisioning.
    assert cog.create_calls == 1
    assert cog.setpw_calls == 1
    assert len(cog.users) == 1


# --------------------------------------------------------------------------- #
# Generic-error indistinguishability (no enumeration oracle)                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["unknown", "expired", "used"])
def test_bad_tokens_all_same_generic_400(fakes, kind):
    table, cog = fakes
    app = _app()
    c = app.test_client()
    token = "tok-" + uuid.uuid4().hex
    if kind == "expired":
        table.seed(token, ttl=-10)
    elif kind == "used":
        th = table.seed(token)
        table.items[th]["status"] = "used"
    # "unknown" -> not seeded at all.

    r = c.post("/api/v1/agent-enrollments/redeem", json={"token": token})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["message"] == "invalid or expired enrollment token"
    # Never provisions on a bad token.
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
    assert cog.create_calls == 2
    assert len(cog.users) == 1
    assert cog.setpw_calls == 2
    assert r2.get_json()["password"] == cog.users["samebot@agents.spec-server.internal"]["password"]


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
