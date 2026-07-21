"""Cognito **CreateAuthChallenge** trigger — mint + email the email-OTP (HA-3).

Entrypoint: ``handler.handler``.

Called when DefineAuthChallenge asks for a ``CUSTOM_CHALLENGE``. For HA-3's
single-round email-OTP chain there is exactly one concrete step:

  * generate a cryptographically-secure 6-digit code,
  * email it to the user via SES (SESv2 SendEmail, using this stack's verified
    From identity + the auth configuration set), and
  * stash the EXPECTED code + an absolute expiry in
    ``privateChallengeParameters`` for VerifyAuthChallengeResponse.

What's public vs private
------------------------
  * ``publicChallengeParameters``  -> sent to the CLIENT. It carries only the
    step name + delivery hint. **Never the code, never the expiry answer.**
  * ``privateChallengeParameters`` -> SERVER-ONLY. Passed straight to
    VerifyAuthChallengeResponse. Holds the expected ``answer`` (the code) and
    ``expires_at`` (epoch seconds). The client never sees these.
  * ``challengeMetadata`` -> echoed into the session so DefineAuthChallenge can
    see which step this was.

The code is NEVER persisted to a database (no DynamoDB row, unlike bird's TOTP
path) — it lives only in the challenge session for its 5-minute lifetime.

Event shape (Cognito CreateAuthChallenge_Authentication):
  event.request.userAttributes = {"email": "...", ...}
  event.response = {"publicChallengeParameters": {...},
                    "privateChallengeParameters": {...},
                    "challengeMetadata": "EMAIL_OTP"}

Env vars:
  OTP_FROM_ADDRESS       — verified SES From address (envelope From).
  OTP_FROM_IDENTITY_ARN  — ARN of the SES identity authorizing the send
                           (domain identity preferred). Passed as
                           FromEmailAddressIdentityArn so a domain-identity send
                           is authorized without per-address verification.
  SES_CONFIG_SET         — SES configuration set name (reputation/bounce metrics).
  OTP_TTL_SECONDS        — code lifetime in seconds (default 300 / 5 min).
  AWS_REGION             — provided by the Lambda runtime.
"""

from __future__ import annotations

import logging
import os

import boto3

import otp

_log = logging.getLogger(__name__)

CUSTOM = "CUSTOM_CHALLENGE"
_ses = None


def _ses_client():
    global _ses
    if _ses is None:
        _ses = boto3.client("sesv2", region_name=os.environ.get("AWS_REGION"))
    return _ses


def _send_email_otp(to_email: str, code: str) -> None:
    """Email the OTP via SESv2. Best-effort: a delivery failure is logged, not
    raised — a raised exception here becomes a Cognito internal error, and the
    private answer is still set, so swallowing the error leaks nothing (an
    attacker who blocks delivery still never learns the code)."""
    sender = os.environ.get("OTP_FROM_ADDRESS", "")
    identity_arn = os.environ.get("OTP_FROM_IDENTITY_ARN", "")
    config_set = os.environ.get("SES_CONFIG_SET", "")

    if not sender or not to_email:
        _log.warning("create_auth: OTP_FROM_ADDRESS or recipient missing; not emailing")
        return

    ttl_min = max(1, otp.otp_ttl_seconds() // 60)
    subject = "Your Spec Server sign-in code"
    body = (
        f"Your Spec Server verification code is {code}.\n\n"
        f"It expires in {ttl_min} minute(s). "
        f"If you did not request it, you can ignore this email."
    )

    kwargs = {
        "FromEmailAddress": sender,
        "Destination": {"ToAddresses": [to_email]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            }
        },
    }
    # Authorize the send against the (domain) identity ARN when provided, and tag
    # it to the auth configuration set for reputation/bounce metrics.
    if identity_arn:
        kwargs["FromEmailAddressIdentityArn"] = identity_arn
    if config_set:
        kwargs["ConfigurationSetName"] = config_set

    try:
        _ses_client().send_email(**kwargs)
    except Exception:  # noqa: BLE001 - delivery failure must not crash the trigger
        _log.exception("create_auth: SES send_email failed")


def handler(event, context=None):
    req = event.get("request", {}) or {}
    attrs = req.get("userAttributes", {}) or {}
    resp = event.setdefault("response", {})

    # Mint a fresh code every time this trigger fires (each retry gets a new one).
    code = otp.new_email_otp()
    expires_at = otp.expiry_epoch()

    _send_email_otp(attrs.get("email", ""), code)

    # PUBLIC — client-visible. Never the code or the expiry answer.
    resp["publicChallengeParameters"] = {
        "challengeType": otp.STEP_EMAIL_OTP,
        "step": otp.STEP_EMAIL_OTP,
        "deliveryMedium": "EMAIL",
    }
    # PRIVATE — server-only, handed to VerifyAuthChallengeResponse. Cognito
    # requires all values to be strings.
    resp["privateChallengeParameters"] = {
        "step": otp.STEP_EMAIL_OTP,
        "answer": code,
        "expires_at": str(expires_at),
    }
    resp["challengeMetadata"] = otp.STEP_EMAIL_OTP

    return event
