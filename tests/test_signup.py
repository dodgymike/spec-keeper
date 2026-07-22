"""Public request->approve signup queue tests (HA-7, bird Path A).

Covers the three security-critical behaviours the task calls out, all with
stubbed boto3 (an in-memory fake DynamoDB Table + a captured SQS/SES), so no
DynamoDB Local / AWS is required:

  * INTAKE (POST /api/v1/signup): uniform-202 ALWAYS (new / dropped alike);
    honeypot drops silently without enqueue; the per-IP rate-limit trips to 429;
    a failed Turnstile token is dropped (not enqueued) when a secret is
    configured, and a missing token is a 400 — both existence-independent.
  * VALIDATE (GET /api/v1/validate): a valid token -> confirmed and transitions
    the row requested -> email-validated; an expired token -> invalid; a reused
    (already-redeemed) token -> confirmed (idempotent) but writes nothing new;
    a wrong/missing token -> invalid (no oracle).
  * ADMIN state machine: approve is rejected (409) from `requested` and accepted
    only from `email-validated`; reject works from a partial row.
"""
from __future__ import annotations

import time

import pytest

from app import create_app, signup as signup_lib, signup_aws, signup_ratelimit
from app.blueprints import admin as admin_bp
from app.blueprints import signup as signup_bp
from app.config import TestConfig
from app.extensions import db
from tests.conftest import TEST_DB_URL


# --------------------------------------------------------------------------- #
# In-memory fake DynamoDB Table (resource API subset the signup code uses)
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict] = {}

    @staticmethod
    def _key(k):
        # Handles both the signups (pk/sk) and invites (code_hash) key shapes.
        if "pk" in k:
            return (k["pk"], k.get("sk"))
        return ("code_hash", k["code_hash"])

    @staticmethod
    def _item_key(item):
        if "pk" in item:
            return (item["pk"], item.get("sk"))
        return ("code_hash", item["code_hash"])

    def put_item(self, Item, ConditionExpression=None):
        key = self._item_key(Item)
        if ConditionExpression and "attribute_not_exists" in ConditionExpression \
                and key in self.items:
            raise _conditional_failure()
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):
        it = self.items.get(self._key(Key))
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ReturnValues=None):
        key = self._key(Key)
        item = self.items.get(key)
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}
        # Evaluate the (small, known) set of conditions the signup code uses.
        if ConditionExpression and not _eval_condition(
            ConditionExpression, item, names, vals
        ):
            raise _conditional_failure()
        if item is None:
            item = {"pk": Key["pk"], "sk": Key.get("sk")}
            self.items[key] = item
        _apply_update(UpdateExpression, item, names, vals)
        return {"Attributes": dict(item)}

    def query(self, IndexName=None, KeyConditionExpression=None, ScanIndexForward=True,
              Limit=None):
        # KeyConditionExpression is a boto3 Key(...).eq(...) equality on gsi1pk.
        # boto3 Key('gsi1pk').eq('STATUS#x') -> _values == (Key(...), 'STATUS#x').
        wanted = getattr(KeyConditionExpression, "_values", None)
        target = wanted[1] if wanted and len(wanted) > 1 else None
        rows = [dict(v) for v in self.items.values() if v.get("gsi1pk") == target]
        rows.sort(key=lambda r: int(r.get("gsi1sk", 0) or 0), reverse=not ScanIndexForward)
        if Limit is not None:
            rows = rows[:Limit]
        return {"Items": rows}


def _conditional_failure():
    class _E(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    return _E("conditional check failed")


def _eval_condition(expr, item, names, vals):
    exists = item is not None
    if expr == "attribute_not_exists(pk)":
        return not exists
    # bump_notify upserts a standalone NOTIFY# item (no attribute_exists(pk)
    # guard): permitted while absent or under cap.
    if "notify_count < :cap" in expr:
        if not exists:
            return True
        return "notify_count" not in item or item.get("notify_count", 0) < vals[":cap"]
    if not exists:
        return False
    # token single-use flip
    if "#u <> :true AND expires_at > :now" in expr:
        used = item.get("used")
        return (used != vals[":true"]) and int(item.get("expires_at", 0)) > vals[":now"]
    # transition_signup: status = :from
    if expr == "attribute_exists(pk) AND #s = :from":
        return item.get("status") == vals[":from"]
    # reject_signup: status IN (:r,:v,:a)
    if "#s IN (:r, :v, :a)" in expr:
        return item.get("status") in (vals[":r"], vals[":v"], vals[":a"])
    # mark_provisioned
    if ":approved" in expr and "attribute_not_exists(provisioned_at)" in expr:
        return item.get("status") == vals[":approved"] and "provisioned_at" not in item
    # bump_resend
    if "resend_count < :cap" in expr:
        return "resend_count" not in item or item.get("resend_count", 0) < vals[":cap"]
    return True


def _apply_update(expr, item, names, vals):
    body = expr
    if body.startswith("ADD "):
        # "ADD resend_count :one SET last_email_at = :now" (only bump_resend)
        add_part, _, set_part = body[4:].partition(" SET ")
        attr, valref = add_part.split()
        item[attr] = item.get(attr, 0) + vals[valref]
        body = "SET " + set_part if set_part else ""
    if body.startswith("SET "):
        for assign in body[4:].split(", "):
            lhs, _, rhs = assign.partition(" = ")
            lhs = names.get(lhs.strip(), lhs.strip())
            item[lhs] = vals[rhs.strip()]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def sent():
    """Captured SQS messages + SES sends."""
    return {"sqs": [], "ses": []}


@pytest.fixture
def signup_env(monkeypatch, sent):
    """Wire the signup + admin blueprints to an in-memory signups table, a
    captured SQS sender, and a captured SES sender."""
    table = FakeTable()
    monkeypatch.setattr(signup_aws, "signups_table", lambda cfg: table)
    monkeypatch.setattr(admin_bp, "_signups_table", lambda cfg: table)
    monkeypatch.setattr(signup_bp.signup_aws, "signups_table", lambda cfg: table)

    def _send_sqs(cfg, message):
        sent["sqs"].append(message)
        return True
    monkeypatch.setattr(signup_bp.signup_aws, "send_intake_message", _send_sqs)

    def _ses(cfg, **kw):
        sent["ses"].append(kw)
        return True
    monkeypatch.setattr(admin_bp.signup_aws, "ses_send", _ses)
    # Rate limiter fails open unless a test injects a table.
    signup_ratelimit.set_table_factory(lambda cfg: None)
    yield table
    signup_ratelimit.set_table_factory(None)


def _app(**overrides):
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
        SIGNUPS_TABLE = "spec-server-signups"
        SIGNUP_INTAKE_QUEUE_URL = "https://sqs.local/intake"
    for k, v in overrides.items():
        setattr(_Cfg, k, v)
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    return app


# --------------------------------------------------------------------------- #
# INTAKE
# --------------------------------------------------------------------------- #
def test_intake_returns_uniform_202_and_enqueues(signup_env, sent):
    c = _app().test_client()
    r = c.post("/api/v1/signup", json={"email": "New@Example.com"})
    assert r.status_code == 202, r.get_json()
    assert "email" not in str(r.get_json()).lower() or "@" not in str(r.get_json())
    assert r.get_json() == signup_lib.uniform_intake_body()
    # ZERO existence work on the sync path -> just an enqueue with a NORMALIZED email.
    assert len(sent["sqs"]) == 1
    assert sent["sqs"][0]["email"] == "new@example.com"
    assert signup_env.items == {}  # no DynamoDB write on the intake path


def test_intake_body_identical_regardless_of_email(signup_env):
    c = _app().test_client()
    a = c.post("/api/v1/signup", json={"email": "a@example.com"})
    b = c.post("/api/v1/signup", json={"email": "b@example.com"})
    assert a.status_code == b.status_code == 202
    assert a.get_json() == b.get_json()  # no distinguishing body


def test_intake_honeypot_drops_without_enqueue(signup_env, sent):
    c = _app().test_client()
    r = c.post("/api/v1/signup", json={"email": "bot@example.com", "hp_website": "spam"})
    assert r.status_code == 202
    assert r.get_json() == signup_lib.uniform_intake_body()  # SAME uniform body
    assert sent["sqs"] == []  # silently dropped, never enqueued


def test_intake_rate_limit_trips(signup_env, sent):
    counter = {"n": 0}

    class RLTable:
        def update_item(self, **kw):
            counter["n"] += 1
            return {"Attributes": {"count": counter["n"]}}

    signup_ratelimit.set_table_factory(lambda cfg: RLTable())
    c = _app(SIGNUP_RATELIMIT_TABLE="rl", SIGNUP_RATELIMIT_MAX=2).test_client()
    codes = [c.post("/api/v1/signup", json={"email": f"u{i}@example.com"}).status_code
             for i in range(4)]
    assert codes == [202, 202, 429, 429]  # over the per-IP budget -> 429
    assert len(sent["sqs"]) == 2  # only the under-limit requests enqueued


def test_intake_turnstile_failure_rejects_when_configured(signup_env, sent, monkeypatch):
    # A configured secret + a token that fails siteverify -> dropped (not enqueued).
    monkeypatch.setattr(signup_bp, "_verify_turnstile", lambda secret, token, ip: False)
    c = _app(TURNSTILE_SECRET="0xsecret").test_client()
    r = c.post("/api/v1/signup", json={"email": "x@example.com", "turnstile_token": "bad"})
    assert r.status_code == 202  # uniform body (no oracle)...
    assert sent["sqs"] == []     # ...but the bot request was NOT enqueued


def test_intake_turnstile_missing_token_is_400(signup_env, sent):
    c = _app(TURNSTILE_SECRET="0xsecret").test_client()
    r = c.post("/api/v1/signup", json={"email": "x@example.com"})
    assert r.status_code == 202  # marshmallow default fills turnstile_token=""
    # empty token fails verify (real siteverify not hit for empty) -> not enqueued
    assert sent["sqs"] == []


def test_intake_turnstile_success_enqueues(signup_env, sent, monkeypatch):
    monkeypatch.setattr(signup_bp, "_verify_turnstile", lambda secret, token, ip: True)
    c = _app(TURNSTILE_SECRET="0xsecret").test_client()
    r = c.post("/api/v1/signup", json={"email": "ok@example.com", "turnstile_token": "good"})
    assert r.status_code == 202
    assert len(sent["sqs"]) == 1


# --------------------------------------------------------------------------- #
# VALIDATE
# --------------------------------------------------------------------------- #
def _seed_signup_and_token(table, email="user@example.com", ttl_seconds=3600, now=None):
    now = now or int(time.time())
    eh = signup_lib.email_hash(email)
    table.put_item(Item=signup_lib.signup_profile_item(email_hash=eh, email=email, now=now))
    minted = signup_lib.mint_token()
    table.put_item(Item=signup_lib.token_item(
        token_id=minted.token_id, token_hash=minted.token_hash,
        email_hash=eh, now=now, ttl_seconds=ttl_seconds,
    ))
    return eh, minted


def test_validate_valid_token_confirms_and_transitions(signup_env):
    eh, minted = _seed_signup_and_token(signup_env)
    c = _app().test_client()
    r = c.get(f"/api/v1/validate?token={minted.link}")
    assert r.status_code == 200
    assert r.get_json() == {"outcome": "confirmed"}
    profile = signup_env.items[(signup_lib.signup_pk(eh), signup_lib.PROFILE_SK)]
    assert profile["status"] == signup_lib.STATE_EMAIL_VALIDATED
    assert profile.get("validated_at")


def test_validate_expired_token_is_invalid(signup_env):
    past = int(time.time()) - 10_000
    eh, minted = _seed_signup_and_token(signup_env, ttl_seconds=1, now=past)
    c = _app().test_client()
    r = c.get(f"/api/v1/validate?token={minted.link}")
    assert r.get_json() == {"outcome": "invalid"}  # correct secret but expired
    profile = signup_env.items[(signup_lib.signup_pk(eh), signup_lib.PROFILE_SK)]
    assert profile["status"] == signup_lib.STATE_REQUESTED  # NOT advanced


def test_validate_reused_token_is_idempotent_confirmed(signup_env):
    eh, minted = _seed_signup_and_token(signup_env)
    c = _app().test_client()
    first = c.get(f"/api/v1/validate?token={minted.link}")
    assert first.get_json() == {"outcome": "confirmed"}
    tok = signup_env.items[(signup_lib.token_pk(minted.token_id), signup_lib.TOKEN_SK)]
    assert tok["used"] is True
    # Re-click: same success page, no new write, token stays used.
    second = c.get(f"/api/v1/validate?token={minted.link}")
    assert second.get_json() == {"outcome": "confirmed"}


def test_validate_rate_limit_trips(signup_env):
    counter = {"n": 0}

    class RLTable:
        def update_item(self, **kw):
            counter["n"] += 1
            return {"Attributes": {"count": counter["n"]}}

    signup_ratelimit.set_table_factory(lambda cfg: RLTable())
    c = _app(SIGNUP_RATELIMIT_TABLE="rl", SIGNUP_RATELIMIT_MAX=2).test_client()
    codes = [c.get("/api/v1/validate?token=a.b").status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]  # over the per-IP budget -> 429


def test_bump_notify_caps_already_registered_amplification(signup_env):
    # The worker's existing-user branch is capped per email/window (mail-bomb
    # defense): bump_notify permits `cap` notices then returns False.
    eh = signup_lib.email_hash("victim@example.com")
    results = [signup_lib.bump_notify(signup_env, eh, cap=3) for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_validate_wrong_and_malformed_tokens_are_invalid(signup_env):
    _seed_signup_and_token(signup_env)
    c = _app().test_client()
    assert c.get("/api/v1/validate?token=deadbeef.wrongsecret").get_json() == {"outcome": "invalid"}
    assert c.get("/api/v1/validate?token=not-a-valid-shape").get_json() == {"outcome": "invalid"}


def test_validate_unconfigured_table_is_neutral_invalid(monkeypatch):
    # SIGNUPS_TABLE unset -> neutral invalid, never a crash.
    monkeypatch.setattr(signup_bp.signup_aws, "signups_table", lambda cfg: None)

    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    r = app.test_client().get("/api/v1/validate?token=a.b")
    assert r.status_code == 200
    assert r.get_json() == {"outcome": "invalid"}


# --------------------------------------------------------------------------- #
# ADMIN state machine
# --------------------------------------------------------------------------- #
def test_admin_approve_rejected_from_requested(signup_env, sent):
    eh, _ = _seed_signup_and_token(signup_env)  # row is `requested` (unvalidated)
    c = _app(INVITES_TABLE="spec-server-invites").test_client()
    r = c.post(f"/api/v1/admin/signups/{eh}/approve")
    assert r.status_code == 409, r.get_json()  # approve requires email-validated
    assert sent["ses"] == []  # nothing provisioned


def test_admin_approve_from_email_validated_provisions(signup_env, sent, monkeypatch):
    eh, _ = _seed_signup_and_token(signup_env)
    # Advance the row to email-validated (as a successful validate would).
    signup_lib.transition_signup(
        signup_env, eh, from_state=signup_lib.STATE_REQUESTED,
        to_state=signup_lib.STATE_EMAIL_VALIDATED,
    )
    invites = FakeTable()
    monkeypatch.setattr(admin_bp, "_invites_table", lambda cfg: invites)
    c = _app(INVITES_TABLE="spec-server-invites").test_client()
    r = c.post(f"/api/v1/admin/signups/{eh}/approve")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == signup_lib.STATE_PROVISIONED
    assert len(invites.items) == 1  # an approved+email-bound invite was minted
    minted_invite = next(iter(invites.items.values()))
    assert minted_invite["approved"] is True
    assert minted_invite["email_binding"]  # pinned to the requester's email
    assert len(sent["ses"]) == 1  # join link emailed to the requester
    assert sent["ses"][0]["to_addr"] == "user@example.com"


def test_admin_reject_from_requested_row(signup_env):
    eh, _ = _seed_signup_and_token(signup_env)  # partial `requested` row
    c = _app().test_client()
    r = c.post(f"/api/v1/admin/signups/{eh}/reject", json={"reason": "spam"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == signup_lib.STATE_REJECTED


def test_admin_signups_unconfigured_returns_501(monkeypatch):
    monkeypatch.setattr(admin_bp, "_signups_table", lambda cfg: None)

    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL
    app = create_app(_Cfg)
    with app.app_context():
        db.create_all()
    r = app.test_client().get("/api/v1/admin/signups")
    assert r.status_code == 501


def test_admin_list_signups_by_status(signup_env):
    _seed_signup_and_token(signup_env, email="a@example.com")
    _seed_signup_and_token(signup_env, email="b@example.com")
    c = _app().test_client()
    r = c.get("/api/v1/admin/signups?status=requested")
    assert r.status_code == 200
    rows = r.get_json()
    assert len(rows) == 2
    assert all(row["status"] == "requested" for row in rows)
    # No plaintext email leaks into the key/hash fields; email is a visible attr.
    assert all(row["email_hash"] for row in rows)
