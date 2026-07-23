"""Cognito **VerifyAuthChallengeResponse** trigger — check the email-OTP (HA-3).

Entrypoint: ``handler.handler``.

Verifies the client's submitted code for the email-OTP step and sets
``response.answerCorrect``. **Fails closed:** an expired code, a missing/unknown
step, missing data, or any error -> ``answerCorrect = False``.

Checks, in order:
  1. The step must be the email-OTP step (anything else is rejected).
  2. The code must not be past its 5-minute expiry (``expires_at`` in the private
     params, stamped by CreateAuthChallenge). An expired code is rejected BEFORE
     the value compare.
  3. Constant-time compare of the submitted code against the ``answer`` that
     CreateAuthChallenge stashed in ``privateChallengeParameters``
     (``otp.otp_matches`` -> ``hmac.compare_digest``).

The expected answer is read ONLY from ``privateChallengeParameters`` (server-set)
— never from ``challengeAnswer`` or ``clientMetadata`` (which are client-supplied).

Event shape (Cognito VerifyAuthChallengeResponse_Authentication):
  event.request.privateChallengeParameters = {"step": ..., "answer": ...,
                                               "expires_at": "<epoch>"}
  event.request.challengeAnswer            = "<the client's submitted code>"
  event.response = {"answerCorrect": bool}
"""

from __future__ import annotations

import logging

import otp
import ratelimit

_log = logging.getLogger(__name__)


def _record_failed_attempt(req) -> None:
    """Increment the per-email cross-session failed-attempt (brute-force) counter
    (SEC-AUTH-2). Called ONLY on a wrong code.

    FAIL-SAFE: this is pure accounting — it runs only on the wrong path and its
    result NEVER changes ``answerCorrect`` (a correct code is verified before this
    is ever reached, so a counter/DynamoDB error can never falsely reject a valid
    code). Any error is swallowed; the counter keys on a SHA-256 hash so no
    plaintext email is stored or logged.
    """
    try:
        attrs = (req.get("userAttributes") or {})
        email = attrs.get("email", "")
        if email:
            ratelimit.incr_fail(email)
    except Exception:  # noqa: BLE001 - accounting must never affect the verdict
        _log.warning("verify_auth: failed-attempt counter unavailable (ignored)")


def handler(event, context=None):
    req = event.get("request", {}) or {}
    resp = event.setdefault("response", {})
    resp["answerCorrect"] = False  # fail closed by default

    try:
        private = req.get("privateChallengeParameters") or {}
        step = private.get("step")
        submitted = (req.get("challengeAnswer") or "").strip()

        if step != otp.STEP_EMAIL_OTP:
            _log.warning("verify_auth: unknown/unset step %r -> reject", step)
            resp["answerCorrect"] = False
            return event

        # Reject anything past the stamped expiry before comparing values.
        if otp.is_expired(private.get("expires_at")):
            _log.info("verify_auth: code expired -> reject")
            resp["answerCorrect"] = False
            return event

        expected = private.get("answer", "")
        correct = otp.otp_matches(expected, submitted)
        resp["answerCorrect"] = correct
        if not correct:
            # Count the WRONG guess toward the cross-session brute-force cap. A
            # correct code is already accepted above and NEVER touches the counter
            # (fail-safe: a counter error cannot falsely reject a valid code).
            _record_failed_attempt(req)
        return event
    except Exception:  # noqa: BLE001 - never throw from the verifier; fail closed
        _log.exception("verify_auth: verification error (failing closed)")
        resp["answerCorrect"] = False
        return event
