"""Signup intake worker Lambda (HA-7, bird Path A / SQS-triggered).

Drains the intake SQS queue (``signups.tf`` ``aws_sqs_queue.signup_intake``) and
does ALL the existence-dependent work the public ``POST /api/v1/signup`` path
deliberately does NOT do. Keeping the branch HERE — off the observable HTTP path,
behind SQS — is the crux of the enumeration-privacy guarantee: the sync intake
route is DB-free and branch-free, so an attacker cannot distinguish
``unknown | pending | already-registered`` by body, status, error text, OR
latency. The differentiated email only ever reaches the email OWNER.

Per record (idempotent, at-least-once safe):
  (a) ALREADY A COGNITO USER — ``cognito-idp:ListUsers`` filtered by email finds
      an account -> SES "you already have an account" to the OWNER, done. No row
      (a full user never enters the queue's DynamoDB state).
  (b) NEW — ``put_signup_if_absent`` (conditional ``attribute_not_exists(pk)`` =
      one row per email ever) creates the ``requested`` row, mints a single-use
      token (stores only its HASH, 24h TTL), and SES-emails the magic link
      ``{VALIDATE_BASE}/validate?token=<token_id.secret>``.
  (c) EXISTING PENDING — the row already exists -> capped resend of the magic
      link via ``bump_resend`` (conditional ``resend_count < cap``). Over cap ->
      silent no-op.

Partial-batch-failure: returns ``{"batchItemFailures": [...]}`` so a poison /
transiently-failing record is retried/DLQ'd alone (the event-source mapping is
configured with ``ReportBatchItemFailures``).

Privacy: returns NOTHING synchronously; structured logs are hashed-only
(``email_hash`` + ``request_id``), never the plaintext email or raw IP.

Env vars: SIGNUPS_TABLE, SIGNUP_PEPPER (optional, HMAC email_hash — MUST match
the app Lambda), SIGNUP_USER_POOL_ID, SIGNUP_VALIDATE_BASE_URL, SIGNUP_RESEND_CAP,
SES_FROM_ADDRESS (FromAddress-pinned by IAM), SES_CONFIG_SET, AWS_REGION.
"""
from __future__ import annotations

import json
import logging
import os

import signup  # vendored copy of app/signup.py

_log = logging.getLogger(__name__)
_log.setLevel(logging.INFO)

_DEFAULT_RESEND_CAP = 3

# Lazy boto3 clients (kept out of import so unit tests could inject their own).
_ddb = None
_ses = None
_cognito = None


def _region() -> str:
    return os.environ.get("AWS_REGION") or "us-east-1"


def _table():
    global _ddb
    if _ddb is None:
        import boto3
        _ddb = boto3.resource("dynamodb", region_name=_region())
    name = os.environ.get("SIGNUPS_TABLE")
    if not name:
        raise RuntimeError("SIGNUPS_TABLE is not configured")
    return _ddb.Table(name)


def _ses_client():
    global _ses
    if _ses is None:
        import boto3
        _ses = boto3.client("sesv2", region_name=_region())
    return _ses


def _cognito_client():
    global _cognito
    if _cognito is None:
        import boto3
        _cognito = boto3.client("cognito-idp", region_name=_region())
    return _cognito


def _pepper():
    return os.environ.get("SIGNUP_PEPPER") or None


def _from_email() -> str:
    addr = os.environ.get("SES_FROM_ADDRESS")
    if not addr:
        raise RuntimeError("SES_FROM_ADDRESS is not configured")
    return addr


def _validate_base() -> str:
    return (os.environ.get("SIGNUP_VALIDATE_BASE_URL") or "").rstrip("/")


def _resend_cap() -> int:
    try:
        return int(os.environ.get("SIGNUP_RESEND_CAP", _DEFAULT_RESEND_CAP))
    except (TypeError, ValueError):
        return _DEFAULT_RESEND_CAP


def _config_set() -> str:
    return (os.environ.get("SES_CONFIG_SET") or "").strip()


# --------------------------------------------------------------------------- #
# SES (owner-only; hashed logs)                                                 #
# --------------------------------------------------------------------------- #
def _send_email(to_addr: str, subject: str, body: str) -> None:
    kwargs = {
        "FromEmailAddress": _from_email(),
        "Destination": {"ToAddresses": [to_addr]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            }
        },
    }
    config_set = _config_set()
    if config_set:
        kwargs["ConfigurationSetName"] = config_set
    _ses_client().send_email(**kwargs)


def _send_magic_link(email: str, link: str) -> None:
    base = _validate_base()
    url = f"{base}/validate?token={link}" if base else f"/validate?token={link}"
    body = (
        "Welcome to Spec Server.\n\n"
        "Confirm this email address to continue your access request:\n\n"
        f"{url}\n\n"
        "This link expires in 24 hours and can be used once. If you did not "
        "request access you can safely ignore this email.\n"
    )
    _send_email(email, "Confirm your email to finish requesting access", body)


def _send_already_registered(email: str) -> None:
    base = _validate_base()
    body = (
        "You already have a Spec Server account for this email address.\n\n"
        f"Sign in here: {base}/\n\n"
        "If you did not just try to request access, no action is needed.\n"
    )
    _send_email(email, "You already have a Spec Server account", body)


# --------------------------------------------------------------------------- #
# Cognito existence check (async, off the observable path)                       #
# --------------------------------------------------------------------------- #
def _is_existing_user(email: str) -> bool:
    pool_id = os.environ.get("SIGNUP_USER_POOL_ID")
    if not pool_id:
        raise RuntimeError("SIGNUP_USER_POOL_ID is not configured")
    # The ListUsers filter value is double-quoted; escape backslash then quote so
    # a crafted local part cannot break out of the quoted value (off the
    # observable path, so never an oracle; a malformed value simply fails -> DLQ).
    safe = email.replace("\\", "\\\\").replace('"', '\\"')
    resp = _cognito_client().list_users(
        UserPoolId=pool_id, Filter=f'email = "{safe}"', Limit=1
    )
    return bool(resp.get("Users"))


def _store_token(table, email_hash: str) -> signup.MintedToken:
    """Mint a single-use magic-link token and persist ONLY its hash (TTL)."""
    minted = signup.mint_token()
    table.put_item(Item=signup.token_item(
        token_id=minted.token_id, token_hash=minted.token_hash, email_hash=email_hash,
    ))
    return minted


# --------------------------------------------------------------------------- #
# Per-record processing                                                          #
# --------------------------------------------------------------------------- #
def _process_record(record: dict) -> None:
    body = json.loads(record.get("body") or "{}")
    raw_email = body.get("email")
    if not raw_email:
        raise ValueError("intake message missing 'email'")

    email = signup.normalize_email(raw_email)
    eh = signup.email_hash(email, pepper=_pepper())
    request_id = body.get("request_id") or ""
    display_name = body.get("display_name")

    table = _table()

    # (a) Already a full Cognito user? Notify the owner, done. No row. The notice
    # is CAPPED per email/window (signup.bump_notify) so a replayed known-
    # registered victim address can't be amplified into a mail-bomb — the notice
    # only ever reaches the owner, but we still bound how often.
    if _is_existing_user(email):
        if signup.bump_notify(table, eh, cap=_resend_cap()):
            _send_already_registered(email)
            _log.info("signup-worker: existing user notified eh=%s rid=%s", eh, request_id)
        else:
            _log.info("signup-worker: existing-user notice capped eh=%s rid=%s", eh, request_id)
        return

    # (b) New signup — conditional create (one row per email ever).
    profile = signup.signup_profile_item(
        email_hash=eh, email=email, display_name=display_name,
    )
    created = signup.put_signup_if_absent(table, profile)
    if not created:
        # (c) Existing pending row -> capped resend of the magic link.
        if not signup.bump_resend(table, eh, cap=_resend_cap()):
            _log.info("signup-worker: resend capped eh=%s rid=%s", eh, request_id)
            return
        minted = _store_token(table, eh)
        _send_magic_link(email, minted.link)
        _log.info("signup-worker: resent magic link eh=%s rid=%s", eh, request_id)
        return

    minted = _store_token(table, eh)
    _send_magic_link(email, minted.link)
    _log.info("signup-worker: new request eh=%s rid=%s", eh, request_id)


def handler(event, context=None):
    """SQS event-source entry point with partial-batch-failure reporting."""
    failures = []
    for record in (event or {}).get("Records", []) or []:
        try:
            _process_record(record)
        except Exception:  # noqa: BLE001 — isolate the poison record for SQS retry
            _log.exception("signup-worker: record failed; reporting for SQS retry")
            mid = record.get("messageId")
            if mid:
                failures.append({"itemIdentifier": mid})
    return {"batchItemFailures": failures}
