"""Cognito **PreSignUp** trigger — invite-only human signup gate (HA-2).

Entrypoint: ``handler.handler``.

Spec Server mirrors the bird project's invite + PreSignUp burn (see
``bird-song-visualisation/upload-platform/lambda/presignup/handler.py`` and
``lambda/common/invites.py``), adapted to this service's group-based approval
model and hash-only invite store.

What it does
------------
  1. Reads the **single-use invite code** from the sign-up event
     (``event.request.clientMetadata.invite_code``, falling back to the legacy
     ``validationData.invite_code``).
  2. **Atomically burns it** against the dedicated DynamoDB invites table
     (``${name_prefix}-invites``, key = ``code_hash``). The invite is stored by
     the SHA-256 HASH of the code — the plaintext code is NEVER persisted — so
     the trigger hashes the presented code and consumes the matching row under a
     single conditional ``UpdateItem`` that flips ``status`` ``active -> used``.
     Because DynamoDB applies a conditional write as one atomic, isolated
     operation per item, two concurrent signups racing the same code can never
     both win: exactly one ``UpdateItem`` satisfies ``status = 'active'`` (it is
     no longer active for the loser). No read-then-write window exists, so there
     is no TOCTOU double-spend gap. Expiry is enforced IN the condition
     (``expires_at > now``) rather than trusting TTL, since DynamoDB TTL deletion
     is best-effort / delayed.
  3. **Email-binding (optional, atomic).** When the invite row carries an
     ``email_binding`` (the SHA-256 hash of the address the admin pinned it to),
     the SAME conditional write additionally requires it to equal the hash of the
     e-mail now registering — so a pinned invite refuses to burn for any other
     address. An OPEN invite (no ``email_binding``) is unaffected. This is done
     as ``(attribute_not_exists(email_binding) OR email_binding = :eb)`` INSIDE
     the burn, so the pin is enforced atomically with the consume (no separate
     read, no TOCTOU, no enumeration oracle — a mismatch is indistinguishable
     from a missing/used/expired code).
  4. On a successful burn it **auto-confirms** the user and **auto-verifies the
     e-mail** (``autoConfirmUser=true`` / ``autoVerifyEmail=true``) — email
     ownership is proven by the first-factor e-mail flow, so no extra click.

Approval is by GROUP, not a status attribute (SHARED CONTRACT with HA-3): this
trigger adds the new user to NO ``spec-*`` group, so they land **pending** and
the app 403s them until an admin adds them to ``spec-readers``. The optional
``approved`` marker on the invite row is preserved for a future PostConfirmation
hook that MAY add an approved invitee straight to ``spec-readers``; this trigger
deliberately does NOT call any Cognito admin API (its role is UpdateItem-only).

Reject contract
---------------
This is a Cognito trigger: to BLOCK a signup we **raise** (Cognito turns the
raised exception into a failed signup). Every rejection — missing, used, expired,
or e-mail-mismatched invite — surfaces as the SAME generic message, so there is
no enumeration oracle. The plaintext invite code is NEVER logged.

Env vars:
  INVITES_TABLE   - DynamoDB invites table name (required)
  AWS_REGION      - region (provided by the Lambda runtime)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

_log = logging.getLogger(__name__)
_log.setLevel(logging.INFO)

_client = None


class InviteError(Exception):
    """Raised when an invite code is missing / used / expired / mis-bound."""


def _dynamo():
    """Lazily build (and cache) the low-level DynamoDB client.

    Low-level client (not resource) so unit tests can drive it with a
    botocore Stubber. Overridable in tests by setting the module global.
    """
    global _client
    if _client is None:
        _client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION"))
    return _client


def _table_name() -> str:
    name = os.environ.get("INVITES_TABLE")
    if not name:
        # Misconfiguration must FAIL CLOSED (block signup), never open the gate.
        raise InviteError("invites table not configured")
    return name


def _hash(value: str) -> str:
    """SHA-256 hex of a UTF-8 string. Used for both the code and the e-mail.

    The invite code is 128 bits of entropy, so a plain (un-peppered) hash is
    sufficient: a stolen table dump cannot be reversed to recover a code, and the
    hash space is far too large to brute-force.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _invite_code(event: dict) -> str:
    req = event.get("request", {}) or {}
    meta = req.get("clientMetadata") or {}
    code = meta.get("invite_code")
    if not code:
        vdata = req.get("validationData") or {}
        code = vdata.get("invite_code")
    return (code or "").strip()


def _registering_email(event: dict) -> str:
    attrs = (event.get("request", {}) or {}).get("userAttributes", {}) or {}
    # In this pool userName IS the email for native users; fall back to it.
    return _norm_email(attrs.get("email") or event.get("userName") or "")


def _burn(code: str, email: str) -> None:
    """Atomically validate AND consume ``code``; raise :class:`InviteError` else.

    A single conditional ``UpdateItem`` flips ``status`` ``active -> used`` and
    stamps ``used_at`` / ``used_email_hash`` under one ``ConditionExpression``:

        attribute_exists(code_hash) AND status = 'active' AND expires_at > now
        AND (attribute_not_exists(email_binding) OR email_binding = :eb)

    The optional last clause makes an e-mail-bound invite refuse to burn for the
    wrong address — atomically, with the same failure as any other rejection.
    """
    if not code:
        raise InviteError("missing invite code")

    now = int(time.time())
    code_hash = _hash(code)
    email_hash = _hash(_norm_email(email))  # empty email -> hash("") never matches a real binding

    try:
        _dynamo().update_item(
            TableName=_table_name(),
            Key={"code_hash": {"S": code_hash}},
            UpdateExpression="SET #s = :used, used_at = :now, used_email_hash = :eb",
            ConditionExpression=(
                "attribute_exists(code_hash) AND #s = :active AND expires_at > :now "
                "AND (attribute_not_exists(email_binding) OR email_binding = :eb)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":used": {"S": "used"},
                ":active": {"S": "active"},
                ":now": {"N": str(now)},
                ":eb": {"S": email_hash},
            },
        )
    except ClientError as exc:
        code_name = exc.response.get("Error", {}).get("Code", "")
        if code_name == "ConditionalCheckFailedException":
            # missing / used / expired / email-mismatch — all indistinguishable.
            raise InviteError("invalid or expired invite code") from exc
        raise


def handler(event, context=None):
    code = _invite_code(event)
    email = _registering_email(event)

    # 1+2+3. Validate + atomically consume the invite (email-binding enforced in
    # the same conditional write). Any failure BLOCKS the signup via a generic
    # raise (Cognito contract; no enumeration oracle; plaintext code never logged).
    try:
        _burn(code, email)
    except InviteError:
        _log.warning("presignup: rejected signup (invalid/used/expired/email-unbound invite)")
        raise Exception("Signup is invite-only. Your invite code is invalid or has expired.")
    except Exception:  # noqa: BLE001 - any infra error must FAIL CLOSED (block).
        _log.exception("presignup: invite burn failed (failing closed)")
        raise Exception("Signup could not be processed. Please try again later.")

    # 4. Auto-confirm + auto-verify email. The new user is added to NO spec-*
    # group (approval is by group, SHARED CONTRACT): they land pending and the
    # app 403s them until an admin adds them to spec-readers.
    resp = event.setdefault("response", {})
    resp["autoConfirmUser"] = True
    resp["autoVerifyEmail"] = True
    resp["autoVerifyPhone"] = False
    return event
