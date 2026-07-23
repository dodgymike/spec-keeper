"""Unit tests for the SEC-AUTH-2 cross-session per-email OTP caps.

These live in the three CUSTOM_AUTH trigger Lambdas (NOT the app):
  * create_auth_lambda  -> email-bomb (code-issuance) cap, before SES send.
  * verify_auth_lambda  -> increments the per-email failed-attempt counter on a
    WRONG code only (never on a correct one).
  * define_auth_lambda  -> reads the failed-attempt counter and refuses to issue
    another challenge once it is over the cross-session cap.

Each handler is self-contained (vendored ``otp.py`` + ``ratelimit.py`` in its
source dir). We load them by path (mirroring ``tests/test_presignup.py``) and
drive the shared ``ratelimit`` module with an in-memory fake low-level DynamoDB
client that evaluates the SAME atomic ``ADD count`` fixed-window semantics the
real table enforces — so the counters are genuinely exercised, not stubbed.

The FAIL-SAFE contract is asserted explicitly: a correct code always verifies
(never falsely rejected), a first code is always sent, and any counter/DynamoDB
error fails open (create/define) or is ignored (verify).
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import time

import pytest
from botocore.exceptions import ClientError

# --- load the three standalone Lambda handlers by path --------------------- #
_TF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infra", "terraform",
)
# Each dir vendors otp.py + ratelimit.py; put them on sys.path so the handlers'
# ``import otp`` / ``import ratelimit`` resolve to the (byte-identical) copies.
for _sub in ("create_auth_lambda", "verify_auth_lambda", "define_auth_lambda"):
    _d = os.path.join(_TF, _sub)
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load(subdir, modname):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(_TF, subdir, "handler.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


create_mod = _load("create_auth_lambda", "create_auth_handler")
verify_mod = _load("verify_auth_lambda", "verify_auth_handler")
define_mod = _load("define_auth_lambda", "define_auth_handler")

# All three handlers share the one ``ratelimit`` module (same module name).
rl = sys.modules["ratelimit"]
otp = sys.modules["otp"]


def _sha(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


class FakeDynamo:
    """In-memory low-level DynamoDB client implementing the counter's UpdateItem
    (atomic ``ADD count :one``) + GetItem, keyed by ``pk``."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.fail_update = False
        self.fail_get = False
        self.update_pks: list[str] = []

    def _boom(self, op):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "boom"}}, op,
        )

    def update_item(self, *, TableName, Key, UpdateExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues, ReturnValues):
        if self.fail_update:
            self._boom("UpdateItem")
        pk = Key["pk"]["S"]
        self.update_pks.append(pk)
        self.counts[pk] = self.counts.get(pk, 0) + 1
        return {"Attributes": {"count": {"N": str(self.counts[pk])}}}

    def get_item(self, *, TableName, Key, ConsistentRead=False):
        if self.fail_get:
            self._boom("GetItem")
        pk = Key["pk"]["S"]
        c = self.counts.get(pk, 0)
        if not c:
            return {}
        return {"Item": {"pk": {"S": pk}, "count": {"N": str(c)}}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeDynamo()
    monkeypatch.setenv("OTP_RATELIMIT_TABLE", "spec-server-signup-ratelimit")
    monkeypatch.setenv("OTP_RATELIMIT_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OTP_SEND_CAP", "5")
    monkeypatch.setenv("OTP_FAIL_CAP", "10")
    monkeypatch.setenv("OTP_FROM_ADDRESS", "noreply@example.com")
    monkeypatch.setattr(rl, "_client", f)
    yield f


class FakeSES:
    def __init__(self):
        self.sends = 0

    def send_email(self, **kwargs):
        self.sends += 1
        return {"MessageId": "m-1"}


@pytest.fixture
def ses(monkeypatch):
    s = FakeSES()
    monkeypatch.setattr(create_mod, "_ses", s)
    return s


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def _create_event(email="alice@example.com"):
    return {"request": {"userAttributes": {"email": email}}, "response": {}}


def _verify_event(answer, submitted, email="alice@example.com", ttl=300):
    return {
        "request": {
            "userAttributes": {"email": email},
            "privateChallengeParameters": {
                "step": otp.STEP_EMAIL_OTP,
                "answer": answer,
                "expires_at": str(int(time.time()) + ttl),
            },
            "challengeAnswer": submitted,
        },
        "response": {},
    }


def _define_event(session=None, email="alice@example.com"):
    return {
        "request": {"userAttributes": {"email": email}, "session": session or []},
        "response": {},
    }


# --------------------------------------------------------------------------- #
# create_auth — email-bomb (issuance) cap
# --------------------------------------------------------------------------- #
def test_create_under_cap_sends_and_sets_answer(fake, ses):
    out = create_mod.handler(_create_event())
    assert ses.sends == 1
    # A valid private answer is still stamped for the flow.
    assert out["response"]["privateChallengeParameters"]["answer"]


def test_create_over_send_cap_stops_emailing(fake, ses):
    # 5/window: first 5 send, the 6th+ are suppressed.
    for _ in range(5):
        create_mod.handler(_create_event())
    assert ses.sends == 5
    out = create_mod.handler(_create_event())  # 6th -> over cap
    assert ses.sends == 5  # no additional email
    # Flow does not crash: a valid private answer is still set (generic failure).
    assert out["response"]["privateChallengeParameters"]["answer"]


def test_create_counter_error_fails_open_and_sends(fake, ses):
    fake.fail_update = True  # counter store unavailable
    create_mod.handler(_create_event())
    assert ses.sends == 1  # FAIL OPEN: legitimate code still emailed


def test_create_counter_keys_are_email_hash_not_plaintext(fake, ses):
    create_mod.handler(_create_event(email="Bob@Example.com"))
    joined = "".join(fake.update_pks)
    assert "bob@example.com" not in joined.lower()
    assert _sha("bob@example.com") in joined  # normalized-email hash present
    assert joined.startswith("otp-send:")


# --------------------------------------------------------------------------- #
# verify_auth — brute-force accounting (wrong-only) + correct always succeeds
# --------------------------------------------------------------------------- #
def test_verify_correct_code_succeeds_and_never_counts(fake):
    out = verify_mod.handler(_verify_event("123456", "123456"))
    assert out["response"]["answerCorrect"] is True
    assert fake.counts == {}  # correct code NEVER touches the counter


def test_verify_wrong_code_fails_and_increments(fake):
    out = verify_mod.handler(_verify_event("123456", "000000"))
    assert out["response"]["answerCorrect"] is False
    key = f"otp-fail:{_sha('alice@example.com')}#{int(time.time()) // 3600}"
    assert fake.counts.get(key) == 1


def test_verify_correct_still_succeeds_even_when_counter_errors(fake):
    # A DDB error on the (unused) counter path must NOT falsely reject a valid code.
    fake.fail_update = True
    out = verify_mod.handler(_verify_event("654321", "654321"))
    assert out["response"]["answerCorrect"] is True


def test_verify_wrong_code_counter_error_is_ignored(fake):
    fake.fail_update = True  # increment blows up
    out = verify_mod.handler(_verify_event("123456", "999999"))
    assert out["response"]["answerCorrect"] is False  # verdict unaffected, no crash


def test_verify_expired_code_does_not_count_as_a_guess(fake):
    out = verify_mod.handler(_verify_event("123456", "123456", ttl=-10))
    assert out["response"]["answerCorrect"] is False
    assert fake.counts == {}  # expiry is not a brute-force guess


# --------------------------------------------------------------------------- #
# define_auth — cross-session brute-force gate
# --------------------------------------------------------------------------- #
def test_define_first_round_issues_challenge(fake):
    out = define_mod.handler(_define_event())
    assert out["response"].get("challengeName") == "CUSTOM_CHALLENGE"
    assert out["response"]["failAuthentication"] is False


def test_define_success_round_issues_tokens_regardless_of_counter(fake):
    # Even with the counter over cap, a SUCCEEDED round still issues tokens.
    key = f"otp-fail:{_sha('alice@example.com')}#{int(time.time()) // 3600}"
    fake.counts[key] = 99
    session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": True}]
    out = define_mod.handler(_define_event(session=session))
    assert out["response"]["issueTokens"] is True


def test_define_over_fail_cap_fails_authentication(fake):
    key = f"otp-fail:{_sha('alice@example.com')}#{int(time.time()) // 3600}"
    fake.counts[key] = 11  # > cap 10
    out = define_mod.handler(_define_event())
    assert out["response"]["failAuthentication"] is True
    assert "challengeName" not in out["response"]


def test_define_counter_read_error_fails_open(fake):
    fake.fail_get = True  # counter read blows up
    out = define_mod.handler(_define_event())
    assert out["response"].get("challengeName") == "CUSTOM_CHALLENGE"
    assert out["response"]["failAuthentication"] is False


# --------------------------------------------------------------------------- #
# End-to-end: the cross-session guarantee (the whole point of SEC-AUTH-2)
# --------------------------------------------------------------------------- #
def test_cross_session_brute_force_is_capped(fake):
    """11 wrong guesses across DISTINCT sessions push the per-email counter over
    the cap; the NEXT session's define_auth then refuses to issue a challenge —
    proving the limit is cross-session, not per-session."""
    email = "victim@example.com"
    for _ in range(11):
        verify_mod.handler(_verify_event("123456", "000000", email=email))
    # A brand-new session (empty) for the same email is now blocked.
    out = define_mod.handler(_define_event(session=[], email=email))
    assert out["response"]["failAuthentication"] is True


def test_cross_session_does_not_block_a_different_email(fake):
    for _ in range(11):
        verify_mod.handler(_verify_event("123456", "000000", email="victim@example.com"))
    # A different victim is unaffected (per-email isolation).
    out = define_mod.handler(_define_event(session=[], email="innocent@example.com"))
    assert out["response"].get("challengeName") == "CUSTOM_CHALLENGE"
