"""Public agent-enrollment REDEEM endpoint (ONBOARD-3).

ONE PUBLIC (no-JWT) route — deliberately NOT gated by ``require_api_key`` so a
brand-new agent that holds NOTHING but a single-use enrollment token can
bootstrap a real Cognito credential:

  * ``POST /api/v1/agent-enrollments/redeem`` — body ``{token}``. Atomically
    BURNS the token (single-use) then PROVISIONS the agent's Cognito user, and
    returns working credentials + a copy-paste setup recipe EXACTLY ONCE.

This is the HIGHEST-RISK surface in the service: a public, unauthenticated route
that mints a real credential. Its safety rests on five load-bearing properties,
each mirrored from a proven precedent:

  1. **Atomic single-use burn** (mirrors the PreSignUp trigger's ``_burn``): one
     conditional ``UpdateItem`` flips ``status`` ``active -> used`` under
     ``status = 'active' AND expires_at > now``. DynamoDB applies a conditional
     write as one atomic, isolated per-item op, so two callers racing the SAME
     token can never both win — exactly one ``UpdateItem`` sees ``active``. No
     read-then-write window, hence no TOCTOU double-spend. Expiry is enforced IN
     the condition (not via best-effort TTL). A double-submit yields exactly one
     success; the loser gets the SAME generic failure as a missing/expired/used
     token (no enumeration oracle).
  2. **Burn BEFORE provision** (DEC: strict single-use is the top priority). If
     provisioning fails after a successful burn, the token stays spent and we
     return 500 — the remedy is to mint a FRESH enrollment (tokens are cheap);
     we never un-burn. See DECISIONS.md.
  3. **Idempotent provisioning** (mirrors scripts/enrol_agents.py): AdminCreateUser
     (SUPPRESS + temp pw) tolerating ``UsernameExistsException`` (re-mint for the
     same agent_name resets the password so the caller still gets working creds)
     -> AdminSetUserPassword(permanent, strong generated pw) -> AdminAddUserToGroup
     ``spec-writers`` (capability tier ONLY) -> ``add_member`` at the enrolled role
     (single project). Never spec-admins; never multiple projects.
  4. **Rate-limited + origin-locked** (reuses the HA-7 signup limiter + origin
     guard) so this public route cannot be hammered.
  5. **No secrets in logs**: the plaintext token and the generated password are
     NEVER logged; every failure surfaces as ONE generic message.

Graceful local dev: when ``AGENT_ENROLLMENTS_TABLE`` is unset the endpoint
returns **501** (mirrors the admin invites/enrollments 501-when-unconfigured
contract); when ``COGNITO_USER_POOL_ID`` is unset it also 501s (no pool to
provision into).
"""
from __future__ import annotations

import logging
import re
import secrets
import string

from flask import current_app, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from . import admin as admin_bp  # reuse the SAME enrollments table + hashing (ONBOARD-2)
from .signup import _client_ip, _origin_ok  # reuse the HA-7 public-path guards
from ..signup_ratelimit import rate_limited
from ..schemas import EnrollRedeemIn, EnrollRedeemOut

_log = logging.getLogger(__name__)

blp = Blueprint(
    "enroll", __name__, url_prefix="/api/v1",
    description="Public agent-enrollment redeem (single-use token -> Cognito credential).",
)

# Generated permanent password: a strong random secret returned to the caller
# ONCE and never stored/logged. Shape mirrors scripts/enrol_agents.py.genpw so it
# always satisfies the pool password policy (upper+lower+digit+symbol).
_PW_BODY_LEN = 22


class EnrollError(Exception):
    """Raised when a redeem token is missing / used / expired (generic to caller)."""


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Ag1!" + "".join(secrets.choice(alphabet) for _ in range(_PW_BODY_LEN))


def _enrollments_table(cfg):
    """Resolve the enrollments DynamoDB Table (or ``None``). Delegates to the
    ONBOARD-2 resolver so both endpoints share ONE table + config knob; a thin
    seam here lets the redeem tests monkeypatch it independently."""
    return admin_bp._enrollments_table(cfg)


def _require_enrollments_table(cfg):
    table = _enrollments_table(cfg)
    if table is None:
        abort(
            501,
            message=(
                "Agent enrollment is not configured on this server "
                "(set AGENT_ENROLLMENTS_TABLE to the enrollments DynamoDB table)."
            ),
        )
    return table


def _cognito_client(cfg):
    """Resolve a boto3 cognito-idp client (or ``None`` when no pool configured).

    Isolated (like admin._cognito_client) so the redeem tests monkeypatch it with
    an in-memory fake and the 501-when-unconfigured path has one source of truth."""
    if not cfg.get("COGNITO_USER_POOL_ID"):
        return None
    import boto3  # lazy: keep boto3 off the import path when enrollment is unused

    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    return boto3.client("cognito-idp", **kwargs)


def _require_pool(cfg):
    client = _cognito_client(cfg)
    pool_id = cfg.get("COGNITO_USER_POOL_ID")
    if client is None or not pool_id:
        abort(
            501,
            message=(
                "Agent enrollment is not configured on this server "
                "(set COGNITO_USER_POOL_ID to provision the agent's Cognito user)."
            ),
        )
    return client, pool_id


def _burn(table, token: str) -> dict:
    """Atomically validate AND consume ``token``; return the burned row or raise.

    One conditional ``UpdateItem`` flips ``status`` ``active -> used`` and stamps
    ``used_at`` under::

        attribute_exists(token_hash) AND status = 'active' AND expires_at > now

    so a missing / already-used / expired token, or a concurrent double-submit
    loser, ALL fail identically (no enumeration oracle). ``ReturnValues=ALL_NEW``
    hands back the burned row's project_slug / role / agent_name for provisioning.
    Values bind via ExpressionAttributeValues (never f-string-formatted)."""
    import time
    from botocore.exceptions import ClientError

    if not token:
        raise EnrollError("invalid or expired enrollment token")

    now = int(time.time())
    token_hash = admin_bp._hash(token)  # SHA-256; identical to the ONBOARD-2 mint

    try:
        resp = table.update_item(
            Key={"token_hash": token_hash},
            UpdateExpression="SET #s = :used, used_at = :now",
            ConditionExpression=(
                "attribute_exists(token_hash) AND #s = :active AND expires_at > :now"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":used": "used",
                ":active": "active",
                ":now": now,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # missing / used / expired / lost-the-race — all indistinguishable.
            raise EnrollError("invalid or expired enrollment token") from exc
        raise
    return resp.get("Attributes", {}) or {}


def _attr(attrs, name):
    for a in attrs or []:
        if a.get("Name") == name:
            return a.get("Value")
    return None


# Per-component bound for the sanitized local-part pieces. Email local-parts cap
# at 64 chars; two <= 20-char pieces + a 16-hex suffix + two dots = 58, under 64.
_LOCALPART_MAX = 20
# Hex width of the disambiguating digest. 16 hex = 64 bits: the tiebreaker only
# matters when sanitization aliases two distinct raw pairs onto the same visible
# local-part, and 64 bits keeps a birthday collision there astronomically remote
# on a credential-isolation boundary (vs 32 bits, which is birthday-thin).
_DIGEST_HEX = 16


def _sanitize_localpart(value: str) -> str:
    """Lowercase and restrict ``value`` to the safe local-part charset
    ``[a-z0-9._-]``, collapsing disallowed runs to a single ``-``, trimming
    leading/trailing separators, and bounding the length. Never empty (an input
    that sanitizes away entirely falls back to ``"x"``)."""
    v = (value or "").strip().lower()
    v = re.sub(r"[^a-z0-9._-]+", "-", v)
    v = v.strip("._-")[:_LOCALPART_MAX].strip("._-")
    return v or "x"


def _provisioned_username(cfg, *, agent_name: str, project_slug: str) -> str:
    """Derive the project-namespaced, unique Cognito username (== email alias) for
    an enrolled agent (ONBOARD-3a).

    A given ``agent_name`` in DIFFERENT projects maps to DIFFERENT Cognito users
    (isolation intent: a redeemed agent is a member of exactly one project), while
    the SAME ``(project_slug, agent_name)`` always maps to the SAME user — so a
    re-enroll is a legitimate password rotation, never a cross-tenant takeover.
    Sanitization (lowercasing / charset-folding / length-bounding) could alias two
    DISTINCT raw inputs onto the same visible local-part, so a deterministic hash
    of the RAW ``(agent_name, project_slug)`` pair is appended: identical pairs are
    always identical (rotation), while distinct pairs collide only on a 64-bit
    birthday coincidence — astronomically remote, and never attacker-targetable
    (mint is project-admin-gated and slugs are unique). The pair is joined with a
    NUL, which cannot appear in either component, so the boundary is unambiguous
    (``("a.b","c")`` and ``("a","b.c")`` hash apart)."""
    domain = cfg.get("ENROLL_AGENT_DOMAIN", "agents.spec-server.internal")
    san_agent = _sanitize_localpart(agent_name)
    san_project = _sanitize_localpart(project_slug)
    digest = admin_bp._hash(f"{agent_name}\x00{project_slug}")[:_DIGEST_HEX]
    return f"{san_agent}.{san_project}.{digest}@{domain}"


def _provision(cfg, client, pool_id, *, agent_name: str, project_slug: str, role: str):
    """Idempotently provision the agent's Cognito user and grant membership.

    Mirrors scripts/enrol_agents.py: AdminCreateUser (SUPPRESS + temp pw) ->
    AdminSetUserPassword(permanent) -> AdminAddUserToGroup(spec-writers). The
    pool uses email-as-username, and the sign-in alias is the PROJECT-NAMESPACED,
    collision-resistant ``_provisioned_username`` (ONBOARD-3a) so the same
    ``agent_name`` in two projects can never map to one shared Cognito user; the
    immutable ``sub`` comes off the AdminCreateUser response (or AdminGetUser on an
    existing user). Returns ``(username_alias, password, sub)``. NEVER logs the
    password."""
    from botocore.exceptions import ClientError

    username = _provisioned_username(cfg, agent_name=agent_name, project_slug=project_slug)
    write_group = cfg.get("AUTH_GROUP_WRITE", "spec-writers")
    password = _generate_password()

    sub = None
    try:
        resp = client.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            TemporaryPassword=password,
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        user = resp.get("User", {}) or {}
        sub = _attr(user.get("Attributes"), "sub") or user.get("Username")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UsernameExistsException":
            raise
        # Idempotent re-enroll: the user already exists (a re-minted token for the
        # same agent_name). Reset its password below so the caller STILL gets
        # working creds; resolve the sub via AdminGetUser.
        got = client.admin_get_user(UserPoolId=pool_id, Username=username)
        sub = _attr(got.get("UserAttributes"), "sub") or got.get("Username")

    # Permanent strong password (works for the freshly-created AND the existing
    # user) so USER_PASSWORD_AUTH returns tokens directly.
    client.admin_set_user_password(
        UserPoolId=pool_id, Username=username, Password=password, Permanent=True
    )
    # Capability tier: spec-writers ONLY — never spec-admins.
    client.admin_add_user_to_group(
        UserPoolId=pool_id, Username=username, GroupName=write_group
    )

    # Single project membership at the enrolled role (idempotent upsert). The
    # sub is the server-resolved Cognito identity, never a client-supplied value.
    current_app.storage.add_member(project_slug, sub or username, agent_name, role)
    return username, password, sub


def _recipe(cfg, *, api_base: str, region, client_id, username: str, project_slug: str) -> dict:
    """A short copy-paste setup guide (metadata only — NEVER the password)."""
    return {
        "1_mint_token": (
            "Mint a Cognito access token from the credentials returned here. Either "
            "use scripts/agent_token.py with env "
            f"POOL_CLIENT_ID={client_id or '<client_id>'} REGION={region or '<region>'} "
            f"COGNITO_USERNAME={username} COGNITO_PASSWORD=<password>, OR call InitiateAuth "
            f"directly: curl -s https://cognito-idp.{region or '<region>'}.amazonaws.com/ "
            "-H 'Content-Type: application/x-amz-json-1.1' "
            "-H 'X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth' "
            "-d '{\"AuthFlow\":\"USER_PASSWORD_AUTH\",\"ClientId\":\"" + (client_id or "<client_id>") + "\","
            "\"AuthParameters\":{\"USERNAME\":\"" + username + "\",\"PASSWORD\":\"<password>\"}}' "
            "(reads AuthenticationResult.AccessToken)."
        ),
        "2_first_call": (
            f"Call GET {api_base}/api/v1/projects with BOTH 'Authorization: Bearer <access_token>' "
            "AND a real 'User-Agent' header. NOTE: Cloudflare 1010-blocks the default python-urllib "
            "User-Agent; scripts/agent_token.py and curl already send a real UA."
        ),
        "3_migrate_local_backlog": (
            "Migrate any local backlog into the cloud: export your local Spec Server backlog "
            "(GET /api/v1/projects/<local-slug>/export) and import it into your enrolled project "
            f"(POST {api_base}/api/v1/projects/{project_slug}/import) with the Bearer token."
        ),
    }


@blp.route("/agent-enrollments/redeem")
class AgentEnrollmentRedeem(MethodView):
    @blp.arguments(EnrollRedeemIn)
    @blp.response(201, EnrollRedeemOut)
    @blp.alt_response(400, description="Invalid/used/expired token (generic — no enumeration oracle).")
    @blp.alt_response(429, description="Rate limited.")
    @blp.alt_response(500, description="Token was spent but provisioning failed — mint a fresh enrollment.")
    @blp.alt_response(501, description="Enrollment not configured on this server.")
    @blp.alt_response(503, description="Transient backend fault before the token was burned — retry.")
    def post(self, data):
        """PUBLIC single-use redeem. Burns the token atomically, provisions the
        agent's Cognito credential, and returns working creds + a recipe ONCE.
        No auth (a brand-new agent holds only the token)."""
        cfg = current_app.config

        # 1. Origin-guard (bounded, opt-in) — existence-independent 403.
        if not _origin_ok(cfg):
            abort(403, message="forbidden")

        # 2. Per-IP rate-limit floor (independent budget; fails open). Keyed on IP,
        # never the token, so it is not an enumeration oracle.
        if rate_limited(cfg, _client_ip(), key_prefix="enr#ip#"):
            abort(429, message="rate_limited")

        # 3. Graceful 501 when unconfigured (no table and/or no pool).
        table = _require_enrollments_table(cfg)
        client, pool_id = _require_pool(cfg)

        token = data["token"]

        # 4. Atomic single-use BURN. A missing/used/expired/raced token ALWAYS
        # surfaces as ConditionalCheckFailed -> EnrollError -> ONE generic 400 (no
        # enumeration oracle). A NON-conditional backend fault (Dynamo throttle/5xx/
        # timeout) leaves the token UN-burned, so it must be reported as a retryable
        # 503 rather than "invalid token" — else a caller discards a still-valid
        # token during a brownout. This split is not an oracle: a genuinely bad
        # token can only ever be a 400, so a 503 means a real backend fault, never
        # a statement about token validity. The plaintext token is NEVER logged.
        try:
            row = _burn(table, token)
        except EnrollError:
            _log.warning("enroll: redeem rejected (invalid/used/expired token)")
            abort(400, message="invalid or expired enrollment token")
        except Exception:  # noqa: BLE001 — transient backend fault; token un-burned
            _log.exception("enroll: backend error burning token (token un-burned)")
            abort(503, message="enrollment is temporarily unavailable; please retry")

        project_slug = row.get("project_slug")
        role = row.get("role")
        agent_name = row.get("agent_name")

        # 5. PROVISION (token already spent). A failure here returns 500 and the
        # token stays burned — the remedy is a fresh enrollment (DECISIONS.md).
        try:
            username, password, _sub = _provision(
                cfg, client, pool_id,
                agent_name=agent_name, project_slug=project_slug, role=role,
            )
        except Exception:  # noqa: BLE001 — token spent; generic 500, never leak
            _log.exception(
                "enroll: provisioning FAILED after burn (agent=%s project=%s) — "
                "token is spent; remedy is to mint a fresh enrollment",
                agent_name, project_slug,
            )
            abort(500, message="enrollment could not be completed; mint a fresh enrollment token")

        region = cfg.get("AWS_REGION")
        client_id = cfg.get("ENROLL_COGNITO_CLIENT_ID")
        api_base = (cfg.get("ENROLL_API_BASE") or "").rstrip("/")

        # 6. Respond ONCE with the working creds + recipe. The password is emitted
        # here and NOWHERE else (never stored, never logged).
        return {
            "username": username,
            "password": password,
            "api_base": api_base,
            "region": region,
            "client_id": client_id,
            "project_slug": project_slug,
            "role": role,
            "recipe": _recipe(
                cfg, api_base=api_base, region=region, client_id=client_id,
                username=username, project_slug=project_slug,
            ),
        }
