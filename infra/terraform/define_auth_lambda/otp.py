"""Shared helper for the email-OTP CUSTOM_AUTH chain (HA-3).

VENDORED COPY — an identical file is dropped into each of the three Lambda
source dirs (define_auth_lambda / create_auth_lambda / verify_auth_lambda) so
each function is a self-contained deployment package with no Lambda layer. The
canonical source is small and audited-in-one-place on purpose: OTP generation
and the constant-time compare must never diverge between the creator and the
verifier. Keep the three copies byte-identical.

Design (mirrors bird's lambda/common/otp.py, trimmed to the FIRST factor only):
  * 6-digit numeric OTP, cryptographically secure (`secrets`).
  * Constant-time compare (`hmac.compare_digest`) — no early-exit timing leak.
  * The code is NEVER persisted to a database: it lives only inside the Cognito
    challenge session (privateChallengeParameters.answer), server-side.
  * A 5-minute (300s) TTL is stamped alongside the answer at create time and
    re-checked at verify time.

TOTP (the bird SECOND factor) is intentionally NOT included here — HA-3 is a
single-round email-OTP chain. TOTP is DEFERRED to a later task.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time

# The single logical step in this chain. Surfaced to the client via
# publicChallengeParameters["challengeType"] and echoed into the session via
# challengeMetadata so DefineAuthChallenge can see which step ran.
STEP_EMAIL_OTP = "EMAIL_OTP"

# 6-digit numeric email OTP.
_OTP_DIGITS = 6

# Default TTL for a code, in seconds (5 minutes). Overridable via OTP_TTL_SECONDS.
_DEFAULT_TTL_SECONDS = 300


def new_email_otp() -> str:
    """Return a fresh cryptographically-random 6-digit numeric OTP (zero-padded).

    Uses ``secrets.randbelow`` (CSPRNG) — never ``random`` — so codes are not
    predictable from prior codes.
    """
    n = secrets.randbelow(10 ** _OTP_DIGITS)
    return f"{n:0{_OTP_DIGITS}d}"


def otp_matches(expected: str, provided: str) -> bool:
    """Constant-time compare of two OTP strings. Empty inputs never match.

    ``hmac.compare_digest`` does not short-circuit on the first differing byte,
    so an attacker cannot recover the code one digit at a time via timing.
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))


def otp_ttl_seconds() -> int:
    """The configured code lifetime in seconds (env OTP_TTL_SECONDS, default 300)."""
    try:
        return int(os.environ.get("OTP_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS


def now_epoch() -> int:
    """Current wall-clock time as integer epoch seconds (UTC)."""
    return int(time.time())


def expiry_epoch(ttl_seconds: int | None = None) -> int:
    """Absolute epoch-seconds instant at which a code minted now must be rejected."""
    ttl = otp_ttl_seconds() if ttl_seconds is None else ttl_seconds
    return now_epoch() + ttl


def is_expired(expires_at: str | int | None, *, at: int | None = None) -> bool:
    """True if ``expires_at`` (epoch seconds, as stored in private params) is past.

    Fails CLOSED: a missing / unparseable expiry is treated as EXPIRED so a
    malformed challenge session can never yield a valid code.
    """
    if expires_at is None or expires_at == "":
        return True
    try:
        deadline = int(expires_at)
    except (TypeError, ValueError):
        return True
    moment = now_epoch() if at is None else at
    return moment > deadline
