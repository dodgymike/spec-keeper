"""Pure primitives for the public request->approve signup queue (HA-7, bird Path A).

This module is the single, boto3-free source of truth for how the signup queue
normalizes + hashes emails, mints / stores / verifies single-use magic-link
validation tokens, guards the signup state machine, and performs race-free
idempotent DynamoDB conditional writes. It is imported by the Flask app (the
public ``POST /api/v1/signup`` intake, the ``GET /api/v1/validate`` redeem, and
the admin signups endpoints) and vendored into the SQS worker Lambda.

Design authority: the bird upload-platform decoupled sign-up queue
(``lambda/common/signup.py`` + ``SIGNUP_QUEUE_DEEPDIVE.md``), adapted and
BOUNDED for the Spec Server:
  * KEPT — the enumeration-privacy crux (§D): the sync intake path does ZERO
    existence work and always returns one uniform 202; all state-dependent work
    (Cognito existence check, row create, email send) happens in the async
    worker off SQS, which an attacker cannot observe or time. Single-use token
    is stored HASH-ONLY, verified in constant time, redeemed with a conditional
    single-use flip. State transitions are guarded + idempotent.
  * DEFERRED (documented, not built) — the S3 WORM audit bucket + the peppered
    ip/ua fingerprints and their Secrets-Manager pepper. ``email_hash`` uses a
    peppered HMAC when a pepper is configured (``SIGNUP_PEPPER``) and falls back
    to a plain SHA-256 for local dev; the email itself is stored only as an
    SSE-KMS-protected attribute value (never a key/GSI segment).

Purity: every DynamoDB executor takes a boto3 ``Table`` argument, so importing
this module needs no AWS and no boto3 — unit tests drive it with an in-memory
fake table.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import unicodedata
from typing import Callable, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants                                                                     #
# --------------------------------------------------------------------------- #
# RFC 5321 caps: 254 total, 64 for the local part.
MAX_EMAIL_LEN = 254
MAX_LOCAL_LEN = 64
MAX_DISPLAY_NAME_LEN = 64

# Validation-token entropy. token_id is the DynamoDB lookup key (128-bit); the
# secret is the unguessable proof (256-bit). token_urlsafe(n) -> ~1.33*n chars.
_TOKEN_ID_BYTES = 16
_TOKEN_SECRET_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # base64url alphabet, no padding

_DEFAULT_TOKEN_TTL_SECONDS = 24 * 3600  # magic link valid 24h
_DEFAULT_SIGNUP_TTL_DAYS = 7            # unvalidated rows expire after 7d

# ---- Signup state machine ------------------------------------------------- #
STATE_REQUESTED = "requested"              # public POST landed (unvalidated)
STATE_EMAIL_VALIDATED = "email-validated"  # user clicked the magic link
STATE_ADMIN_APPROVED = "admin-approved"    # admin approved (validated only)
STATE_PROVISIONED = "provisioned"          # invite minted (terminal)
STATE_REJECTED = "rejected"                # admin rejected (terminal)
STATE_EXPIRED = "expired"                  # TTL/sweep (terminal)

TERMINAL_STATES = frozenset({STATE_PROVISIONED, STATE_REJECTED, STATE_EXPIRED})

# Approve is ONLY valid from email-validated: a partial (`requested`) row may be
# viewed and rejected but never approved without validating the email first.
ALLOWED_TRANSITIONS = {
    STATE_REQUESTED: frozenset({STATE_EMAIL_VALIDATED, STATE_REJECTED, STATE_EXPIRED}),
    STATE_EMAIL_VALIDATED: frozenset({STATE_ADMIN_APPROVED, STATE_REJECTED}),
    STATE_ADMIN_APPROVED: frozenset({STATE_PROVISIONED, STATE_REJECTED}),
    STATE_PROVISIONED: frozenset(),
    STATE_REJECTED: frozenset(),
    STATE_EXPIRED: frozenset(),
}
_REJECTABLE_STATES = frozenset(
    {STATE_REQUESTED, STATE_EMAIL_VALIDATED, STATE_ADMIN_APPROVED}
)

VALID_STATES = (
    STATE_REQUESTED, STATE_EMAIL_VALIDATED, STATE_ADMIN_APPROVED,
    STATE_PROVISIONED, STATE_REJECTED, STATE_EXPIRED,
)

# ---- DynamoDB key shape (dedicated ``${name_prefix}-signups`` table) ------- #
PROFILE_SK = "PROFILE"
TOKEN_SK = "TOKEN"

# ---- Uniform public intake response (§D) ---------------------------------- #
UNIFORM_INTAKE_STATUS = 202
# A module-level constant *builder* (not a shared mutable dict) so a buggy caller
# can never mutate the body every other request sees.
_UNIFORM_INTAKE_MESSAGE = (
    "If that email can sign up, we've emailed you a confirmation link. "
    "Check your inbox."
)


def uniform_intake_body() -> dict:
    """Return a fresh copy of the single fixed 202 intake body."""
    return {"message": _UNIFORM_INTAKE_MESSAGE}


class SignupError(Exception):
    """Base error. HTTP callers must never leak ``str(exc)`` (enumeration oracle)."""


class StateError(SignupError):
    """Raised for an illegal state transition (e.g. approve-from-unvalidated)."""


# --------------------------------------------------------------------------- #
# Email normalization + hashing                                                 #
# --------------------------------------------------------------------------- #
def normalize_email(email: str) -> str:
    """Return a deterministic canonical form of ``email`` for hashing.

    NFC-normalize, trim, reject internal whitespace, require exactly one ``@``
    with non-empty local + domain, lowercase, IDNA/punycode the domain
    (best-effort), enforce the RFC 5321 length caps. Provider-specific alias
    folding (gmail dots / ``+tag``) is deliberately NOT applied. Raises
    :class:`SignupError` on malformed / oversized input.
    """
    if not isinstance(email, str):
        raise SignupError("email must be a string")
    e = unicodedata.normalize("NFC", email).strip()
    if not e:
        raise SignupError("email is empty")
    if len(e) > MAX_EMAIL_LEN:
        raise SignupError("email too long")
    if any(ch.isspace() for ch in e):
        raise SignupError("email contains whitespace")
    if e.count("@") != 1:
        raise SignupError("email must contain exactly one @")
    local, domain = e.split("@")
    if not local or not domain:
        raise SignupError("email local part and domain must be non-empty")
    if len(local) > MAX_LOCAL_LEN:
        raise SignupError("email local part too long")
    local = local.lower()
    domain = domain.lower()
    try:
        domain = domain.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return f"{local}@{domain}"


def email_hash(email: str, *, pepper: Optional[str] = None) -> str:
    """Return a stable, non-plaintext hash of ``email`` for use as a table key.

    When a ``pepper`` is configured (``SIGNUP_PEPPER``) the hash is
    ``HMAC-SHA256(pepper, normalize(email))`` — a pepper defeats offline
    dictionary reversal of a leaked table. When no pepper is configured (local
    dev / bounded default) it falls back to a plain ``SHA-256(normalize(email))``.
    Either way the plaintext email is NEVER embedded, and normalization makes the
    hash case/whitespace-insensitive.
    """
    normalized = normalize_email(email)
    if pepper:
        key = pepper.encode("utf-8") if isinstance(pepper, str) else bytes(pepper)
        return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Validation token: mint (plaintext link) / store (hash only) / verify (CT)     #
# --------------------------------------------------------------------------- #
class MintedToken(tuple):
    """``(link, token_id, token_hash)`` — ``link`` (``token_id.secret``) is the
    ONLY place the secret exists in plaintext; only ``token_hash`` is persisted."""

    __slots__ = ()

    def __new__(cls, link: str, token_id: str, token_hash: str):
        return super().__new__(cls, (link, token_id, token_hash))

    link = property(lambda self: self[0])
    token_id = property(lambda self: self[1])
    token_hash = property(lambda self: self[2])


def hash_token_secret(secret: str) -> str:
    """``sha256(secret)`` hex. The secret is 256-bit random, so a plain
    (un-peppered) hash is sufficient — there is no dictionary to attack."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_token() -> MintedToken:
    """Mint a fresh single-use validation token. Returns the plaintext ``link``
    for the email plus the ``token_id`` + ``token_hash`` to persist. The secret
    itself is never stored, so a leaked table cannot forge links."""
    token_id = secrets.token_urlsafe(_TOKEN_ID_BYTES)
    secret = secrets.token_urlsafe(_TOKEN_SECRET_BYTES)
    return MintedToken(f"{token_id}.{secret}", token_id, hash_token_secret(secret))


def split_token(presented: str) -> Optional[Tuple[str, str]]:
    """Split ``token_id.secret`` into ``(token_id, secret)``; ``None`` for
    anything malformed. Never raises — a malformed token must produce the SAME
    neutral outcome as a valid-but-wrong one (no oracle)."""
    if not isinstance(presented, str):
        return None
    parts = presented.split(".")
    if len(parts) != 2:
        return None
    token_id, secret = parts
    if not token_id or not secret:
        return None
    if not _TOKEN_RE.match(token_id) or not _TOKEN_RE.match(secret):
        return None
    return token_id, secret


def verify_token(secret: str, stored_hash: str) -> bool:
    """Constant-time verify a presented ``secret`` against the stored hash
    (``hmac.compare_digest`` — no timing oracle). Empty inputs never match."""
    if not secret or not stored_hash:
        return False
    return hmac.compare_digest(hash_token_secret(secret), str(stored_hash))


# --------------------------------------------------------------------------- #
# DynamoDB key + item builders                                                  #
# --------------------------------------------------------------------------- #
def signup_pk(eh: str) -> str:
    return f"SIGNUP#{eh}"


def token_pk(token_id: str) -> str:
    return f"TOKEN#{token_id}"


def status_gsi_pk(status: str) -> str:
    return f"STATUS#{status}"


def signup_profile_item(
    *,
    email_hash: str,
    email: str,
    display_name: Optional[str] = None,
    now: Optional[int] = None,
    ttl_days: int = _DEFAULT_SIGNUP_TTL_DAYS,
) -> dict:
    """Build the ``requested`` profile item for a conditional first insert.

    ``email`` is the ONLY plaintext email in the item and it lives in an
    attribute VALUE (SSE-KMS at rest) — never in a key or the status GSI.
    ``gsi1pk``/``gsi1sk`` drive the admin "list by status, newest-first" query.
    """
    now = _now(now)
    if display_name is not None:
        display_name = str(display_name).strip()[:MAX_DISPLAY_NAME_LEN]
    item = {
        "pk": signup_pk(email_hash),
        "sk": PROFILE_SK,
        "status": STATE_REQUESTED,
        "email": email,
        "email_hash": email_hash,
        "created_at": now,
        "updated_at": now,
        "resend_count": 0,
        "ttl": now + int(ttl_days) * 86400,
        "gsi1pk": status_gsi_pk(STATE_REQUESTED),
        "gsi1sk": now,
    }
    if display_name:
        item["display_name"] = display_name
    return item


def token_item(
    *,
    token_id: str,
    token_hash: str,
    email_hash: str,
    now: Optional[int] = None,
    ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
) -> dict:
    """Build the validation-token item: stores ONLY ``token_hash`` (never the
    secret), the ``email_hash`` link, an ``expires_at``/``ttl`` for auto-expiry,
    and ``used=False`` for the single-use conditional flip."""
    now = _now(now)
    exp = now + int(ttl_seconds)
    return {
        "pk": token_pk(token_id),
        "sk": TOKEN_SK,
        "token_hash": token_hash,
        "email_hash": email_hash,
        "expires_at": exp,
        "ttl": exp,
        "used": False,
    }


# --------------------------------------------------------------------------- #
# State-machine guards (pure)                                                    #
# --------------------------------------------------------------------------- #
def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def can_approve(state: str) -> bool:
    """Approval is valid ONLY from ``email-validated``."""
    return state == STATE_EMAIL_VALIDATED


def can_reject(state: str) -> bool:
    return state in _REJECTABLE_STATES


def require_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise StateError(f"illegal signup transition {from_state!r} -> {to_state!r}")


# --------------------------------------------------------------------------- #
# DynamoDB conditional-write executors (race-free, idempotent)                  #
# --------------------------------------------------------------------------- #
# Each takes a boto3 DynamoDB ``Table``, does a SINGLE conditional write, and
# returns a bool: True if THIS call effected the change, False if a
# ConditionalCheckFailedException means it was already done (idempotent no-op /
# lost the race). Any other error propagates.
def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _cond_write(fn: Callable) -> bool:
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 — narrow to the conditional case below
        if _is_conditional_failure(exc):
            return False
        raise


def put_signup_if_absent(table, item: dict) -> bool:
    """Insert the profile item iff absent (``PutItem`` guarded by
    ``attribute_not_exists(pk)``) — one row per email ever; a duplicate returns
    ``False``."""
    return _cond_write(
        lambda: table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    )


def consume_token(table, token_id: str, secret: str, *, now: Optional[int] = None) -> bool:
    """Constant-time verify + single-use flip of a validation token.

    Returns ``True`` ONLY if this call redeemed a valid+unused+unexpired token;
    ``False`` for missing / mismatched / expired / already-used (the caller
    renders the SAME neutral outcome for every ``False``). A single conditional
    ``UpdateItem`` guarded by ``#used <> :true AND expires_at > :now`` means two
    concurrent clicks can never both win.
    """
    token_id = token_id or ""
    if not token_id or not secret:
        return False
    now = _now(now)
    resp = table.get_item(Key={"pk": token_pk(token_id), "sk": TOKEN_SK})
    row = resp.get("Item")
    if not row:
        return False
    if not verify_token(secret, row.get("token_hash", "")):
        return False
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": token_pk(token_id), "sk": TOKEN_SK},
            UpdateExpression="SET #u = :true",
            ConditionExpression="attribute_exists(pk) AND #u <> :true AND expires_at > :now",
            ExpressionAttributeNames={"#u": "used"},
            ExpressionAttributeValues={":true": True, ":now": now},
        )
    )


def transition_signup(
    table,
    email_hash: str,
    *,
    from_state: str,
    to_state: str,
    extra_set: Optional[dict] = None,
    now: Optional[int] = None,
) -> bool:
    """Conditionally move a profile ``from_state -> to_state`` (single UpdateItem).

    :func:`require_transition` rejects illegal moves BEFORE the DB is touched;
    the ``status = :from`` condition makes a re-run an idempotent ``False``.
    ``updated_at``, ``status`` and ``gsi1pk`` are always refreshed."""
    require_transition(from_state, to_state)
    now = _now(now)
    names = {"#s": "status"}
    values = {":from": from_state, ":to": to_state, ":now": now,
              ":gpk": status_gsi_pk(to_state)}
    set_parts = ["#s = :to", "updated_at = :now", "gsi1pk = :gpk"]
    for i, (k, v) in enumerate(sorted((extra_set or {}).items())):
        nk, vk = f"#e{i}", f":e{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": signup_pk(email_hash), "sk": PROFILE_SK},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(pk) AND #s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    )


def reject_signup(
    table, email_hash: str, *, actor: str, reason: str = "", now: Optional[int] = None
) -> bool:
    """Reject a profile from any non-terminal admin-visible state (idempotent
    ``False`` for a re-reject or a terminal row)."""
    now = _now(now)
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": signup_pk(email_hash), "sk": PROFILE_SK},
            UpdateExpression=(
                "SET #s = :rej, updated_at = :now, gsi1pk = :gpk, "
                "rejected_by = :actor, reject_reason = :reason"
            ),
            ConditionExpression="attribute_exists(pk) AND #s IN (:r, :v, :a)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":rej": STATE_REJECTED,
                ":now": now,
                ":gpk": status_gsi_pk(STATE_REJECTED),
                ":actor": actor,
                ":reason": reason or "",
                ":r": STATE_REQUESTED,
                ":v": STATE_EMAIL_VALIDATED,
                ":a": STATE_ADMIN_APPROVED,
            },
        )
    )


def mark_provisioned(
    table, email_hash: str, *, extra_set: Optional[dict] = None, now: Optional[int] = None
) -> bool:
    """Stamp ``provisioned`` exactly-once. Guarded by
    ``attribute_not_exists(provisioned_at)`` AND ``status = admin-approved`` so a
    retry / concurrent racer returns ``False`` (never provisions twice)."""
    now = _now(now)
    names = {"#s": "status"}
    values = {
        ":prov": STATE_PROVISIONED,
        ":approved": STATE_ADMIN_APPROVED,
        ":now": now,
        ":gpk": status_gsi_pk(STATE_PROVISIONED),
    }
    set_parts = ["#s = :prov", "provisioned_at = :now", "updated_at = :now", "gsi1pk = :gpk"]
    for i, (k, v) in enumerate(sorted((extra_set or {}).items())):
        nk, vk = f"#p{i}", f":p{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": signup_pk(email_hash), "sk": PROFILE_SK},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=(
                "attribute_exists(pk) AND #s = :approved "
                "AND attribute_not_exists(provisioned_at)"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    )


def bump_notify(
    table, email_hash: str, *, cap: int, ttl_seconds: int = 86400, now: Optional[int] = None
) -> bool:
    """Async cap on the "you already have an account" notice to a REGISTERED email.

    An existing full user has NO signups profile row (the queue never stores one
    for them), so the ``bump_resend`` counter cannot bound the notice — without a
    cap, an attacker replaying a known-registered victim's address (rotating IPs /
    while the fail-open IP limiter is degraded) turns each accepted intake into
    one email to the victim (a mail-bomb amplifier, not an oracle: the send only
    ever reaches the owner). This bounds it with a standalone TTL'd counter item
    (``NOTIFY#<eh>``): a single conditional upsert increments iff under ``cap``;
    the ``ttl`` resets the window so the cap is per-window, not lifetime. Returns
    ``True`` if a notice is permitted (and counts it), ``False`` if over cap.
    Enforced ONLY in the async worker and returns nothing to the client, so it is
    never an enumeration oracle."""
    now = _now(now)
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": f"NOTIFY#{email_hash}", "sk": "NOTIFY"},
            UpdateExpression="ADD notify_count :one SET #ttl = :ttl, updated_at = :now",
            ConditionExpression="attribute_not_exists(notify_count) OR notify_count < :cap",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":one": 1, ":cap": int(cap), ":ttl": now + int(ttl_seconds), ":now": now,
            },
        )
    )


def bump_resend(table, email_hash: str, *, cap: int, now: Optional[int] = None) -> bool:
    """Async per-email resend limiter — increment iff under ``cap`` (returns
    ``True`` if a resend is permitted, ``False`` if over cap). Enforced ONLY in
    the async worker and returns nothing to the client, so it is never an
    enumeration oracle."""
    now = _now(now)
    return _cond_write(
        lambda: table.update_item(
            Key={"pk": signup_pk(email_hash), "sk": PROFILE_SK},
            UpdateExpression="ADD resend_count :one SET last_email_at = :now",
            ConditionExpression=(
                "attribute_exists(pk) AND (attribute_not_exists(resend_count) "
                "OR resend_count < :cap)"
            ),
            ExpressionAttributeValues={":one": 1, ":now": now, ":cap": int(cap)},
        )
    )


# --------------------------------------------------------------------------- #
# small internals                                                               #
# --------------------------------------------------------------------------- #
def _now(now: Optional[int]) -> int:
    return int(time.time()) if now is None else int(now)
