"""Unit tests for the Cognito PreSignUp invite-burn trigger (HA-2).

The handler lives in the terraform Lambda source dir
(``infra/terraform/presignup_lambda/handler.py``) and is fully self-contained
(no ``common`` package). We drive it with an in-memory fake DynamoDB client that
evaluates the SAME conditional-write semantics the real table enforces, so the
burn (valid / already-used / expired / email-mismatch) is genuinely exercised —
not merely stubbed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import time

import pytest
from botocore.exceptions import ClientError

# --- import the standalone Lambda handler by path -------------------------- #
_HANDLER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infra", "terraform", "presignup_lambda", "handler.py",
)
_spec = importlib.util.spec_from_file_location("presignup_handler", _HANDLER_PATH)
handler_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler_mod)


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


class FakeDynamo:
    """Minimal in-memory DynamoDB client for the burn UpdateItem.

    Evaluates exactly the handler's ConditionExpression:
        attribute_exists(code_hash) AND status = :active AND expires_at > :now
        AND (attribute_not_exists(email_binding) OR email_binding = :eb)
    Items are stored in DynamoDB attribute-value form keyed by code_hash.
    """

    def __init__(self):
        self.store: dict[str, dict] = {}

    def seed(self, code_hash, status="active", expires_in=3600, email_binding=None):
        item = {
            "code_hash": {"S": code_hash},
            "status": {"S": status},
            "expires_at": {"N": str(int(time.time()) + expires_in)},
        }
        if email_binding is not None:
            item["email_binding"] = {"S": email_binding}
        self.store[code_hash] = item

    def update_item(self, *, TableName, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues):
        code_hash = Key["code_hash"]["S"]
        item = self.store.get(code_hash)
        vals = ExpressionAttributeValues
        ok = (
            item is not None
            and item.get("status", {}).get("S") == vals[":active"]["S"]
            and int(item.get("expires_at", {}).get("N", "0")) > int(vals[":now"]["N"])
            and (
                "email_binding" not in item
                or item["email_binding"]["S"] == vals[":eb"]["S"]
            )
        )
        if not ok:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "The conditional request failed"}},
                "UpdateItem",
            )
        item["status"] = {"S": vals[":used"]["S"]}
        item["used_at"] = {"N": vals[":now"]["N"]}
        item["used_email_hash"] = {"S": vals[":eb"]["S"]}
        return {}


@pytest.fixture
def fake(monkeypatch):
    f = FakeDynamo()
    monkeypatch.setenv("INVITES_TABLE", "spec-server-invites")
    monkeypatch.setattr(handler_mod, "_client", f)
    return f


def _event(code, email="alice@example.com"):
    return {
        "userName": email,
        "request": {
            "userAttributes": {"email": email, "sub": "sub-123"},
            "clientMetadata": {"invite_code": code},
        },
        "response": {"autoConfirmUser": False, "autoVerifyEmail": False},
    }


def test_valid_burn_confirms_and_marks_used(fake):
    code = "good-code"
    fake.seed(_sha(code))
    out = handler_mod.handler(_event(code))
    assert out["response"]["autoConfirmUser"] is True
    assert out["response"]["autoVerifyEmail"] is True
    # The code was consumed atomically: active -> used.
    assert fake.store[_sha(code)]["status"]["S"] == "used"


def test_already_used_raises(fake):
    code = "spent-code"
    fake.seed(_sha(code), status="used")
    with pytest.raises(Exception) as exc:
        handler_mod.handler(_event(code))
    assert "invite" in str(exc.value).lower()


def test_expired_raises(fake):
    code = "old-code"
    fake.seed(_sha(code), expires_in=-10)  # already expired
    with pytest.raises(Exception):
        handler_mod.handler(_event(code))


def test_email_mismatch_raises(fake):
    code = "bound-code"
    fake.seed(_sha(code), email_binding=_sha("owner@example.com"))
    with pytest.raises(Exception):
        handler_mod.handler(_event(code, email="intruder@example.com"))
    # Bound invite survives a wrong-email attempt (not burned).
    assert fake.store[_sha(code)]["status"]["S"] == "active"


def test_email_bound_match_burns(fake):
    code = "bound-ok"
    fake.seed(_sha(code), email_binding=_sha("owner@example.com"))
    out = handler_mod.handler(_event(code, email="Owner@Example.com"))  # case-normalized
    assert out["response"]["autoConfirmUser"] is True
    assert fake.store[_sha(code)]["status"]["S"] == "used"


def test_missing_code_raises(fake):
    ev = _event("x")
    ev["request"]["clientMetadata"] = {}
    with pytest.raises(Exception):
        handler_mod.handler(ev)


def test_unknown_code_raises(fake):
    # Nothing seeded for this hash.
    with pytest.raises(Exception):
        handler_mod.handler(_event("never-issued"))
