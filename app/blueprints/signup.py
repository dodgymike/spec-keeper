"""Public request->approve signup queue — the unauthenticated surface (HA-7).

Two PUBLIC (no-auth) routes, deliberately NOT gated by ``require_api_key`` so the
public request page can reach them (the API-Gateway route is wired with
authorization NONE in signups.tf):

  * ``POST /api/v1/signup`` — the uniform-202 anti-enumeration INTAKE. It does
    ZERO existence work (no DynamoDB read, no Cognito, no email) and ALWAYS
    returns the identical 202 body, so an attacker cannot distinguish
    unknown / pending / already-registered by body, status, error text, OR
    latency. Order of the cheap guards: origin-guard -> honeypot -> per-IP
    rate-limit -> optional Turnstile -> enqueue to SQS. The differentiated work
    (existence check, row create, magic-link email) happens in the async worker
    Lambda off SQS, which an attacker can neither observe nor time.

  * ``GET /api/v1/validate?token=`` — redeem the single-use magic link. Constant
    -time hash compare + a conditional single-use flip transition the row
    ``requested`` -> ``email-validated``. Every failure mode (missing / malformed
    / wrong / expired / already-used) folds into the SAME neutral ``invalid``
    outcome (no token-guessing oracle).

Graceful local dev: when the SQS queue / signups table are unconfigured the
intake still returns its uniform 202 (without enqueuing) and validate returns the
neutral ``invalid`` — never a stack trace, never an existence signal.
"""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request
import uuid

from flask import current_app, request
from flask.views import MethodView
from flask_smorest import Blueprint

from .. import signup
from .. import signup_aws
from ..signup_ratelimit import rate_limited
from ..schemas import (
    SignupAcceptedOut,
    SignupIn,
    ValidateOut,
    ValidateQuery,
)

_log = logging.getLogger(__name__)

blp = Blueprint(
    "signup", __name__, url_prefix="/api/v1",
    description="Public request->approve signup queue (uniform-202 intake + magic-link validation).",
)

_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_SITEVERIFY_TIMEOUT_SECONDS = 5


# --------------------------------------------------------------------------- #
# Request helpers                                                               #
# --------------------------------------------------------------------------- #
def _client_ip() -> str:
    """Prefer the real client IP behind a CDN (CF-Connecting-IP / first
    X-Forwarded-For hop) over the immediate peer."""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _origin_ok(cfg) -> bool:
    """Bounded origin-guard: when SIGNUP_ENFORCE_ORIGIN is on AND an allow-list is
    configured, the Origin (or Referer host) must match. Off/empty => allow (dev)."""
    if not cfg.get("SIGNUP_ENFORCE_ORIGIN"):
        return True
    allowed = cfg.get("SIGNUP_ALLOWED_ORIGINS") or []
    if not allowed:
        return True
    origin = request.headers.get("Origin")
    if not origin:
        referer = request.headers.get("Referer")
        if referer:
            parts = urllib.parse.urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"
    return bool(origin and origin in allowed)


def _verify_turnstile(secret: str, token: str, remote_ip: str) -> bool:
    """Server-side siteverify of a Turnstile token. Returns True only on a genuine
    ``success: true``; ANY error fails closed (treated as a bot). Never raises."""
    if not token:
        return False
    fields = {"secret": secret, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        _SITEVERIFY_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_SITEVERIFY_TIMEOUT_SECONDS) as resp:
            import json
            payload = json.loads(resp.read().decode("utf-8"))
        return bool(payload.get("success") is True)
    except Exception:  # noqa: BLE001 — any failure fails closed
        _log.warning("signup: Turnstile siteverify failed; failing closed")
        return False


# --------------------------------------------------------------------------- #
# Intake                                                                        #
# --------------------------------------------------------------------------- #
@blp.route("/signup")
class SignupIntake(MethodView):
    @blp.arguments(SignupIn)
    @blp.response(202, SignupAcceptedOut)
    @blp.alt_response(400, description="Malformed or rejected request (existence-independent).")
    def post(self, data):
        """PUBLIC uniform-202 intake. Does ZERO existence work; always returns the
        identical accepted body. No auth (public request page)."""
        cfg = current_app.config
        request_id = uuid.uuid4().hex

        # 1. Origin-guard (bounded, opt-in). Existence-independent 403.
        if not _origin_ok(cfg):
            from flask_smorest import abort
            abort(403, message="forbidden")

        # 2. Honeypot FIRST — a non-empty hidden field is a naive bot. Silently
        # drop (no enqueue) and return the SAME uniform 202 (no oracle).
        if str(data.get("hp_website") or "").strip():
            return signup.uniform_intake_body()

        remote_ip = _client_ip()

        # 3. Per-IP rate-limit floor (email-independent, fails open). Over-limit ->
        # uniform 429 (still not an oracle — keyed on IP, never the email).
        if rate_limited(cfg, remote_ip):
            from flask_smorest import abort
            abort(429, message="rate_limited")

        # 4. Turnstile — verified ONLY when a secret is configured on THIS
        # deployment (never from client input). A failed/absent token is a bot:
        # silently drop, return the SAME uniform 202 (no enqueue, no oracle).
        turnstile_secret = cfg.get("TURNSTILE_SECRET")
        if turnstile_secret:
            token = str(data.get("turnstile_token") or "")
            if not _verify_turnstile(turnstile_secret, token, remote_ip):
                return signup.uniform_intake_body()

        # 5. Branch-free enqueue. Normalize the email (existence-independent
        # coarse 400 on a grossly malformed value) and SendMessage — NO DynamoDB,
        # NO Cognito, NO email, NO existence branch. The worker does all
        # state-dependent work off SQS.
        try:
            normalized = signup.normalize_email(data["email"])
        except signup.SignupError:
            from flask_smorest import abort
            abort(400, message="invalid request")

        message = {
            "email": normalized,
            "display_name": (data.get("display_name") or ""),
            "ip": remote_ip,
            "ua": request.headers.get("User-Agent", ""),
            "request_id": request_id,
            "requested_at": signup._now(None),
        }
        try:
            signup_aws.send_intake_message(cfg, message)
        except Exception:  # noqa: BLE001 — existence-independent infra failure
            _log.exception("signup: intake enqueue failed")
            # Still return the uniform body: an SQS blip must not become an oracle.

        return signup.uniform_intake_body()


# --------------------------------------------------------------------------- #
# Validate (magic-link redeem)                                                  #
# --------------------------------------------------------------------------- #
@blp.route("/validate")
class SignupValidate(MethodView):
    @blp.arguments(ValidateQuery, location="query")
    @blp.response(200, ValidateOut)
    def get(self, query):
        """PUBLIC magic-link redeem. Constant-time compare + single-use flip.
        Every failure folds into the neutral ``invalid`` (no oracle). No auth."""
        cfg = current_app.config
        # Per-IP floor (independent budget from intake). Token-guessing-exposed
        # public route; the 256-bit secret already makes brute force infeasible,
        # this caps volume. Fails open when the counter table is unconfigured.
        if rate_limited(cfg, _client_ip(), key_prefix="val#ip#"):
            from flask_smorest import abort
            abort(429, message="rate_limited")

        table = signup_aws.signups_table(cfg)
        if table is None:
            return {"outcome": "invalid"}  # unconfigured -> neutral, graceful

        parsed = signup.split_token(query.get("token", ""))
        if parsed is None:
            return {"outcome": "invalid"}
        token_id, secret = parsed

        try:
            confirmed = _redeem(table, token_id, secret)
        except Exception:  # noqa: BLE001 — never leak internals / never an oracle
            _log.exception("signup: unexpected error redeeming token")
            return {"outcome": "invalid"}
        return {"outcome": "confirmed" if confirmed else "invalid"}


def _redeem(table, token_id: str, secret: str) -> bool:
    """Return True iff the outcome must be ``confirmed``.

    An idempotent re-click of an already-redeemed token is ``confirmed`` (same
    success page, no new write). A fresh redemption does the conditional
    single-use flip then guard-transitions the profile
    ``requested`` -> ``email-validated`` (idempotent no-op if already moved on).
    A wrong/expired/missing token is ``invalid``."""
    import time as _time

    resp = table.get_item(Key={"pk": signup.token_pk(token_id), "sk": signup.TOKEN_SK})
    row = resp.get("Item")
    if not row:
        return False
    if not signup.verify_token(secret, row.get("token_hash", "")):
        return False

    now = int(_time.time())
    email_hash = row.get("email_hash", "")
    if row.get("used"):
        return True  # idempotent re-click
    if int(row.get("expires_at", 0) or 0) <= now:
        return False  # correct secret but expired

    # The conditional flip absorbs concurrent double-clicks: losing the race to a
    # concurrent duplicate is still ``confirmed`` from this caller's point of view
    # (their token IS valid), so the return is unconditionally True here.
    signup.consume_token(table, token_id, secret, now=now)
    if email_hash:
        signup.transition_signup(
            table, email_hash,
            from_state=signup.STATE_REQUESTED,
            to_state=signup.STATE_EMAIL_VALIDATED,
            extra_set={"validated_at": now},
            now=now,
        )
    return True
