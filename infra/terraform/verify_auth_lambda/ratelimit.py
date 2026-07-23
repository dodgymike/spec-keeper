"""Cross-session per-email rate-limit counter for the email-OTP CUSTOM_AUTH chain
(SEC-AUTH-2).

VENDORED COPY — an identical file is dropped into each of the three custom-auth
Lambda source dirs (define_auth_lambda / create_auth_lambda / verify_auth_lambda)
so each function stays a self-contained deployment package with no Lambda layer.
Keep the three copies byte-identical (the same rule as otp.py).

WHY it exists
-------------
Cognito ``InitiateAuth CUSTOM_AUTH`` hits cognito-idp directly (not behind the
API-GW throttle) and each wrong guess re-emails a fresh code. The per-session
3-attempt cap (define_auth) does NOT bound an attacker who opens many sessions.
This adds a CROSS-SESSION, per-email cap for two abuses:

  * code-issuance (email-bomb)  -> ``otp-send:<hash>`` counter, checked by
    create_auth BEFORE emailing.
  * failed-verification (brute-force) -> ``otp-fail:<hash>`` counter, incremented
    by verify_auth on a WRONG code and read by define_auth's issuance gate.

It reuses the EXISTING per-IP signup rate-limit DynamoDB table (a distinct key
namespace), mirroring ``app/signup_ratelimit.py``'s atomic fixed-window primitive:
a single ``UpdateItem`` ``ADD count :one`` on a ``pk = <key>#<window>`` item, with
a TTL two windows out so the bucket self-GCs. No new table or IAM beyond
Get/UpdateItem on that table.

PRIVACY: the email is NEVER stored or logged in plaintext — the key carries only a
SHA-256 hash of the normalized (trim + lowercase) address, matching the presignup
email-hash pattern.

FAIL-SAFE posture is decided BY EACH CALLER, not here: this module simply raises
on a missing/unconfigured table or any DynamoDB error, and every call site wraps
it and chooses fail-open (create_auth issuance, define_auth gate) or best-effort
(verify_auth increment) so a counter blip NEVER blocks a legitimate first code or
falsely rejects a correct one.
"""

from __future__ import annotations

import hashlib
import os
import time

# Namespaces keep the two counters (and any other pk on the shared table) apart.
_NS_SEND = "otp-send"
_NS_FAIL = "otp-fail"

_DEFAULT_WINDOW_SECONDS = 3600
_DEFAULT_SEND_CAP = 5
_DEFAULT_FAIL_CAP = 10

# Process-cached low-level DynamoDB client; tests inject a fake by setting this
# module global (mirrors presignup_lambda's ``_client`` test seam).
_client = None


def _dynamo():
    """Lazily build (and cache) the low-level DynamoDB client. boto3 is imported
    lazily so module import stays cheap when the limiter is unconfigured."""
    global _client
    if _client is None:
        import boto3  # lazy: keep boto3 off the import path when unused

        _client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION"))
    return _client


def email_hash(email: str) -> str:
    """SHA-256 hex of the normalized email (trim + lowercase).

    The plaintext address is NEVER stored or logged — only this hash keys the
    counter (same pattern as the presignup invite email-binding hash).
    """
    norm = (email or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _table_name() -> str:
    return os.environ.get("OTP_RATELIMIT_TABLE", "")


def _int_env(name: str, default: int) -> int:
    try:
        n = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def window_seconds() -> int:
    """Fixed-window length in seconds (env OTP_RATELIMIT_WINDOW_SECONDS, def 3600)."""
    return _int_env("OTP_RATELIMIT_WINDOW_SECONDS", _DEFAULT_WINDOW_SECONDS)


def send_cap() -> int:
    """Max code emails per email per window (env OTP_SEND_CAP, default 5)."""
    return _int_env("OTP_SEND_CAP", _DEFAULT_SEND_CAP)


def fail_cap() -> int:
    """Max wrong-code attempts per email per window (env OTP_FAIL_CAP, default 10)."""
    return _int_env("OTP_FAIL_CAP", _DEFAULT_FAIL_CAP)


def _window_pk(namespace: str, ehash: str, now: int) -> str:
    return f"{namespace}:{ehash}#{now // window_seconds()}"


def _incr(namespace: str, ehash: str, *, now: int | None = None) -> int:
    """Atomically increment and RETURN the counter for ``(namespace, ehash)`` in
    the current fixed window. One ``UpdateItem`` ``ADD count :one`` returns the new
    value; a fresh ttl (two windows out) lets DynamoDB GC the bucket. RAISES on a
    missing table or any DynamoDB error — the caller decides the fail posture."""
    table = _table_name()
    if not table:
        raise RuntimeError("OTP_RATELIMIT_TABLE not configured")
    now = int(time.time()) if now is None else int(now)
    resp = _dynamo().update_item(
        TableName=table,
        Key={"pk": {"S": _window_pk(namespace, ehash, now)}},
        UpdateExpression="ADD #c :one SET #ttl = :ttl",
        ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":one": {"N": "1"},
            ":ttl": {"N": str(now + window_seconds() * 2)},
        },
        ReturnValues="UPDATED_NEW",
    )
    return int(resp.get("Attributes", {}).get("count", {}).get("N", "0"))


def incr_send(email: str, *, now: int | None = None) -> int:
    """Increment the per-email code-issuance (email-bomb) counter; return the new
    count. RAISES on any error — create_auth wraps this and FAILS OPEN (sends)."""
    return _incr(_NS_SEND, email_hash(email), now=now)


def incr_fail(email: str, *, now: int | None = None) -> int:
    """Increment the per-email failed-verification (brute-force) counter; return
    the new count. RAISES on any error — verify_auth wraps this best-effort (a
    counter error never changes the verdict)."""
    return _incr(_NS_FAIL, email_hash(email), now=now)


def read_fail(email: str, *, now: int | None = None) -> int:
    """Return the current per-email failed-verification count for THIS window
    WITHOUT incrementing (used by define_auth's brute-force gate). RAISES on a
    missing table or any DynamoDB error — define_auth wraps this and FAILS OPEN."""
    table = _table_name()
    if not table:
        raise RuntimeError("OTP_RATELIMIT_TABLE not configured")
    now = int(time.time()) if now is None else int(now)
    resp = _dynamo().get_item(
        TableName=table,
        Key={"pk": {"S": _window_pk(_NS_FAIL, email_hash(email), now)}},
        ConsistentRead=True,
    )
    item = resp.get("Item") or {}
    return int(item.get("count", {}).get("N", "0"))
