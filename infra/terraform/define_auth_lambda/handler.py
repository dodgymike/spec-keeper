"""Cognito **DefineAuthChallenge** trigger — the email-OTP chain state machine (HA-3).

Entrypoint: ``handler.handler``.

This is a SINGLE-ROUND email-OTP chain (the FIRST authentication factor for
onboarding + passkey recovery):

    email-OTP  ->  issue tokens

The bird reference runs a two-round ``email-OTP -> TOTP`` chain; that second
factor is DEFERRED for HA-3. This trigger therefore never looks up a per-user
TOTP secret — it just drives one CUSTOM_CHALLENGE round with a bounded number of
retries.

Decision table (each session entry has ``challengeName`` / ``challengeResult``):
  * empty session (no custom rounds yet)     -> issue CUSTOM_CHALLENGE (email-OTP)
  * a custom round SUCCEEDED                  -> issueTokens = true
  * < MAX_ATTEMPTS custom rounds, last failed -> issue CUSTOM_CHALLENGE (retry:
                                                 CreateAuthChallenge mints a fresh
                                                 code and re-emails it)
  * >= MAX_ATTEMPTS failed custom rounds       -> failAuthentication = true

FAIL CLOSED: any unexpected error while inspecting the session ends the flow with
``failAuthentication = true`` / ``issueTokens = false`` — an evaluation error must
never accidentally hand out tokens.

Event shape (Cognito DefineAuthChallenge_Authentication):
  event.request.session = [
    {"challengeName": "CUSTOM_CHALLENGE", "challengeResult": true/false,
     "challengeMetadata": "EMAIL_OTP"}, ...
  ]
  event.response = {"challengeName": ..., "issueTokens": bool,
                    "failAuthentication": bool}

Env vars: OTP_MAX_ATTEMPTS (optional, default 3).
"""

from __future__ import annotations

import logging
import os

from otp import STEP_EMAIL_OTP

_log = logging.getLogger(__name__)

CUSTOM = "CUSTOM_CHALLENGE"
_DEFAULT_MAX_ATTEMPTS = 3


def _max_attempts() -> int:
    try:
        n = int(os.environ.get("OTP_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ATTEMPTS
    # Never allow an unbounded / non-positive attempt budget.
    return n if n >= 1 else _DEFAULT_MAX_ATTEMPTS


def handler(event, context=None):
    resp = event.setdefault("response", {})
    # Safe defaults: neither issue tokens nor fail until we decide below.
    resp["issueTokens"] = False
    resp["failAuthentication"] = False

    try:
        req = event.get("request", {}) or {}
        session = req.get("session") or []

        # Only our CUSTOM_CHALLENGE rounds count toward the email-OTP chain.
        custom_rounds = [s for s in session if s.get("challengeName") == CUSTOM]

        # A correct answer on any round completes the (single-round) chain.
        if any(s.get("challengeResult") is True for s in custom_rounds):
            resp["issueTokens"] = True
            return event

        failed = sum(1 for s in custom_rounds if s.get("challengeResult") is False)

        # Out of attempts -> deny (fail closed).
        if failed >= _max_attempts():
            _log.info(
                "define_auth: %d failed attempt(s) >= max %d -> failAuthentication",
                failed,
                _max_attempts(),
            )
            resp["failAuthentication"] = True
            return event

        # First round, or a retry within budget -> issue (another) email-OTP
        # challenge. CreateAuthChallenge mints a fresh code on each issuance.
        resp["challengeName"] = CUSTOM
        # `challengeMetadata` is set by CreateAuthChallenge; the marker below is
        # only for readability of intent.
        _log.debug("define_auth: issuing %s challenge (%s)", CUSTOM, STEP_EMAIL_OTP)
        return event
    except Exception:  # noqa: BLE001 - fail CLOSED on any evaluation error
        _log.exception("define_auth: error evaluating session — failing closed")
        resp["issueTokens"] = False
        resp["failAuthentication"] = True
        return event
