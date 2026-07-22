"""Admin surface — invite-only human signup management (HA-2).

Endpoints (BOTH gated on the ``spec-admins`` group via the existing group authz,
``require_api_key(required="admin")``):

  * ``POST /api/v1/admin/invites`` — mint a single-use invite. Generates a random
    128-bit code, stores ONLY its SHA-256 ``code_hash`` in the dedicated invites
    DynamoDB table (``status='active'``, optional ``email_binding`` hash, TTL),
    and returns the plaintext ``code`` + a join URL **once**. The plaintext code
    is never stored or logged.
  * ``GET /api/v1/admin/invites`` — list active invites (``code_hash`` / status /
    expiry / email-bound / approved) for a future admin UI. NEVER the plaintext.

This is deliberately NOT part of the storage abstraction: invites are an auth
artifact living in their own table (``${name_prefix}-invites``), reached via
boto3. When ``INVITES_TABLE`` is unset (the local-dev default) both endpoints
return **501** so a local run without the table is graceful.

The PreSignUp Lambda (``infra/terraform/presignup_lambda/handler.py``) is the
consumer: it hashes the presented code and atomically burns the matching row.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from .. import signup as signup_lib  # HA-7 signup-queue primitives
from .. import signup_aws  # HA-7 boto3 glue (signups table / SES)
from ..helpers import current_identity, require_api_key
from ..schemas import (
    AdminApproveIn,
    AdminRejectIn,
    AdminSignupOut,
    AdminSignupsQuery,
    AdminUserOut,
    AdminUsersQuery,
    InviteIn,
    InviteMintOut,
    InviteOut,
)

blp = Blueprint(
    "admin", __name__, url_prefix="/api/v1/admin",
    description="Admin-only operations (invite-only human signup; user lifecycle).",
)

# 16 bytes = 128 bits of entropy, url-safe (~22 chars). Doubles as the unguessable
# ?code= URL segment AND the server-validated gate (burned by PreSignUp).
_CODE_BYTES = 16


def _hash(value: str) -> str:
    """SHA-256 hex of a UTF-8 string (used for the code and the bound e-mail).

    The code carries 128 bits of entropy, so a plain (un-peppered) hash is
    sufficient: a stolen table dump cannot be reversed to recover a live code.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _invites_table(cfg):
    """Return a boto3 DynamoDB Table for the invites store, or ``None`` if unset.

    Isolated in one function so unit tests can monkeypatch it with an in-memory
    fake (no DynamoDB Local needed) and so the 501-when-unconfigured path is the
    single source of truth.
    """
    name = cfg.get("INVITES_TABLE")
    if not name:
        return None
    import boto3  # lazy: keeps boto3 off the import path when invites are unused

    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    if cfg.get("DYNAMODB_ENDPOINT_URL"):
        kwargs["endpoint_url"] = cfg["DYNAMODB_ENDPOINT_URL"]
    return boto3.resource("dynamodb", **kwargs).Table(name)


def _require_table(cfg):
    table = _invites_table(cfg)
    if table is None:
        abort(
            501,
            message=(
                "Invites are not configured on this server "
                "(set INVITES_TABLE to the invites DynamoDB table)."
            ),
        )
    return table


@blp.route("/invites")
class InvitesCollection(MethodView):
    @blp.response(200, InviteOut(many=True))
    def get(self):
        """List ACTIVE invites (hashes/status/expiry only — never the plaintext).

        Admin-only. Low-volume + TTL-swept, so a full scan filtered in-process is
        adequate; no plaintext code is ever stored, listed, or logged.
        """
        require_api_key(required="admin")
        table = _require_table(current_app.config)

        items: list[dict] = []
        resp = table.scan()
        items.extend(resp.get("Items", []))
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))

        return [
            {
                "code_hash": it.get("code_hash"),
                "status": it.get("status"),
                "created_at": int(it["created_at"]) if it.get("created_at") is not None else None,
                "expires_at": int(it["expires_at"]) if it.get("expires_at") is not None else None,
                "email_bound": bool(it.get("email_binding")),
                "approved": bool(it.get("approved")),
            }
            for it in items
            if it.get("status") == "active"
        ]

    @blp.arguments(InviteIn)
    @blp.response(201, InviteMintOut)
    def post(self, data):
        """Mint a single-use invite; return the plaintext code + join URL ONCE.

        Only the SHA-256 ``code_hash`` is persisted (with status/TTL and, when an
        ``email`` is supplied, an ``email_binding`` hash pinning it to that
        address). The plaintext ``code`` is returned in this response and never
        stored or logged.
        """
        require_api_key(required="admin")
        cfg = current_app.config
        table = _require_table(cfg)

        code = secrets.token_urlsafe(_CODE_BYTES)
        code_hash = _hash(code)
        now = int(time.time())
        ttl_days = data.get("ttl_days") or cfg.get("INVITE_TTL_DAYS", 14)
        expires_at = now + int(ttl_days) * 86400

        item = {
            "code_hash": code_hash,
            "status": "active",
            "created_at": now,
            "expires_at": expires_at,
            "approved": bool(data.get("approved")),
        }
        email = _norm_email(data.get("email"))
        if email:
            item["email_binding"] = _hash(email)

        # Collision guard (astronomically unlikely for a 128-bit code): never
        # overwrite an existing row.
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(code_hash)")

        base = (cfg.get("INVITE_JOIN_BASE_URL") or "").rstrip("/")
        join_url = f"{base}/join?code={code}" if base else f"/join?code={code}"

        return {
            "code": code,
            "join_url": join_url,
            "code_hash": code_hash,
            "expires_at": expires_at,
            "email_bound": bool(email),
            "approved": bool(data.get("approved")),
        }


# =========================================================================== #
# HA-5 — Admin user lifecycle (approve / reject / block / delete / promote).
#
# All endpoints are spec-admins-gated (require_api_key(required="admin")). Human
# approval is by GROUP: a pending human sits in NO spec-* group; approving adds
# spec-readers (or spec-writers), promoting adds spec-admins, and rejecting or
# blocking disables the Cognito user AND strips its spec-* groups. These apply
# equally to AGENT users (they are Cognito users too).
#
# The pool is reached via boto3 cognito-idp using the COGNITO_USER_POOL_ID knob;
# when unset (local-dev default) every endpoint returns 501 gracefully — mirrors
# the invites 501-when-unconfigured contract above. No token/password is ever
# read back or logged; only identity + group membership + enabled state.
# =========================================================================== #

# Bound the ListUsers walk so a large pool can never turn one admin request into
# an unbounded scan (cost/latency). Pages are 60 (Cognito's per-page max).
_USERS_PAGE = 60
_USERS_MAX = 500


def _cognito_client(cfg):
    """Return a boto3 cognito-idp client, or ``None`` when no pool is configured.

    Isolated (like ``_invites_table``) so tests monkeypatch it with an in-memory
    fake and the 501-when-unconfigured path has a single source of truth."""
    if not cfg.get("COGNITO_USER_POOL_ID"):
        return None
    import boto3  # lazy: keep boto3 off the import path when user-admin is unused

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
                "User administration is not configured on this server "
                "(set COGNITO_USER_POOL_ID to the Cognito user pool id)."
            ),
        )
    return client, pool_id


def _group_names(cfg) -> dict[str, str]:
    return {
        "admin": cfg.get("AUTH_GROUP_ADMIN", "spec-admins"),
        "write": cfg.get("AUTH_GROUP_WRITE", "spec-writers"),
        "read": cfg.get("AUTH_GROUP_READ", "spec-readers"),
    }


def _spec_groups(cfg) -> set[str]:
    return set(_group_names(cfg).values())


def _attr(attrs, name):
    for a in attrs or []:
        if a.get("Name") == name:
            return a.get("Value")
    return None


def _user_groups(client, pool_id, username) -> list[str]:
    groups: list[str] = []
    kwargs = {"UserPoolId": pool_id, "Username": username, "Limit": _USERS_PAGE}
    while True:
        resp = client.admin_list_groups_for_user(**kwargs)
        groups.extend(g["GroupName"] for g in resp.get("Groups", []) if g.get("GroupName"))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return groups


def _list_users(client, pool_id) -> list[dict]:
    users: list[dict] = []
    kwargs = {"UserPoolId": pool_id, "Limit": _USERS_PAGE}
    while True:
        resp = client.list_users(**kwargs)
        users.extend(resp.get("Users", []))
        token = resp.get("PaginationToken")
        if not token or len(users) >= _USERS_MAX:
            break
        kwargs["PaginationToken"] = token
    return users[:_USERS_MAX]


def _user_dto(client, pool_id, user, spec_groups) -> dict:
    username = user.get("Username")
    groups = _user_groups(client, pool_id, username)
    created = user.get("UserCreateDate")
    return {
        "username": username,
        "email": _attr(user.get("Attributes"), "email"),
        "enabled": bool(user.get("Enabled", True)),
        "status": "active" if any(g in spec_groups for g in groups) else "pending",
        "groups": groups,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def _get_user_or_404(client, pool_id, username) -> dict:
    from botocore.exceptions import ClientError

    try:
        return client.admin_get_user(UserPoolId=pool_id, Username=username)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            abort(404, message=f"User '{username}' not found.")
        raise


def _require_self_guard_auth(cfg) -> None:
    """Fail closed for self-protected mutations when caller identity is unverifiable.

    The self-lockout guardrail depends on ``current_identity()``, which is
    populated ONLY on the Cognito JWT path (from the verified token). Under
    static ``API_KEYS`` auth (issuer unset) every caller is anonymous to us, so a
    self-block / self-delete / self-demote would slip past the guard: refuse
    rather than run it blind. Fully-open local dev (no auth at all) keeps working
    — it has no pool in practice and 501s before reaching here anyway."""
    if cfg.get("API_KEYS") and not cfg.get("COGNITO_ISSUER"):
        abort(
            501,
            message=(
                "Self-protected user administration (block/reject/delete/demote) "
                "requires Cognito JWT auth so the caller can be identified; "
                "set COGNITO_ISSUER."
            ),
        )


def _caller_is_target(username, target) -> bool:
    """True when the verified caller IS the user being acted on (self-action).

    Compared against the *verified* token identity only. When auth is off
    (local dev) there is no identity, so this is False — but the endpoints 501
    without a pool anyway, so no self-lockout path is reachable un-authenticated."""
    caller = current_identity()
    if not caller:
        return False
    cu, cs = caller.get("username"), caller.get("sub")
    if cu and (cu == username or cu == target.get("Username")):
        return True
    target_sub = _attr(target.get("UserAttributes"), "sub")
    return bool(cs and target_sub and cs == target_sub)


def _admin_usernames(client, pool_id, admin_group) -> set[str]:
    names: set[str] = set()
    kwargs = {"UserPoolId": pool_id, "GroupName": admin_group, "Limit": _USERS_PAGE}
    while True:
        resp = client.list_users_in_group(**kwargs)
        names.update(u.get("Username") for u in resp.get("Users", []) if u.get("Username"))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return names


def _disable_and_strip(client, pool_id, username, cfg) -> None:
    """Disable the Cognito user and remove every spec-* group it holds."""
    client.admin_disable_user(UserPoolId=pool_id, Username=username)
    spec = _spec_groups(cfg)
    for g in _user_groups(client, pool_id, username):
        if g in spec:
            client.admin_remove_user_from_group(
                UserPoolId=pool_id, Username=username, GroupName=g
            )


def _block_or_reject(username):
    """Shared reject/block: refuse self-action, then disable + strip groups."""
    require_api_key(required="admin")
    cfg = current_app.config
    client, pool_id = _require_pool(cfg)
    _require_self_guard_auth(cfg)
    target = _get_user_or_404(client, pool_id, username)
    if _caller_is_target(username, target):
        abort(409, message="Refusing to block/reject yourself — that risks locking all admins out.")
    _disable_and_strip(client, pool_id, username, cfg)
    return ""


@blp.route("/users")
class UsersCollection(MethodView):
    @blp.arguments(AdminUsersQuery, location="query")
    @blp.response(200, AdminUserOut(many=True))
    def get(self, query):
        """List pool users (username/email/enabled/groups/derived status/created).

        ``?status=pending|active`` filters by derived status (pending = no spec-*
        group). Bounded walk (<= 500 users) — never an unbounded scan."""
        require_api_key(required="admin")
        cfg = current_app.config
        client, pool_id = _require_pool(cfg)
        spec_groups = _spec_groups(cfg)
        dtos = [_user_dto(client, pool_id, u, spec_groups) for u in _list_users(client, pool_id)]
        status = query.get("status")
        if status:
            dtos = [d for d in dtos if d["status"] == status]
        return dtos


@blp.route("/users/<username>")
class UserItem(MethodView):
    @blp.response(204)
    def delete(self, username):
        """Hard-delete a user (AdminDeleteUser). Refuses to delete yourself."""
        require_api_key(required="admin")
        cfg = current_app.config
        client, pool_id = _require_pool(cfg)
        _require_self_guard_auth(cfg)
        target = _get_user_or_404(client, pool_id, username)
        if _caller_is_target(username, target):
            abort(409, message="Refusing to delete yourself — that risks locking all admins out.")
        client.admin_delete_user(UserPoolId=pool_id, Username=username)
        return ""


@blp.route("/users/<username>/approve")
class ApproveUser(MethodView):
    @blp.arguments(AdminApproveIn)
    @blp.response(204)
    def post(self, data, username):
        """Approve a pending user by adding a read/write group (default spec-readers)."""
        require_api_key(required="admin")
        cfg = current_app.config
        client, pool_id = _require_pool(cfg)
        _get_user_or_404(client, pool_id, username)
        group = data.get("group") or cfg.get("AUTH_GROUP_READ", "spec-readers")
        client.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
        return ""


@blp.route("/users/<username>/reject")
class RejectUser(MethodView):
    @blp.response(204)
    def post(self, username):
        """Reject a user: disable the Cognito account and strip its spec-* groups."""
        return _block_or_reject(username)


@blp.route("/users/<username>/block")
class BlockUser(MethodView):
    @blp.response(204)
    def post(self, username):
        """Block a user: disable the Cognito account and strip its spec-* groups."""
        return _block_or_reject(username)


@blp.route("/users/<username>/unblock")
class UnblockUser(MethodView):
    @blp.response(204)
    def post(self, username):
        """Re-enable a previously blocked/rejected user (AdminEnableUser).

        Groups are NOT restored — re-grant access via /approve or /promote."""
        require_api_key(required="admin")
        client, pool_id = _require_pool(current_app.config)
        _get_user_or_404(client, pool_id, username)
        client.admin_enable_user(UserPoolId=pool_id, Username=username)
        return ""


@blp.route("/users/<username>/promote")
class PromoteUser(MethodView):
    @blp.response(204)
    def post(self, username):
        """Promote a user to admin (add spec-admins)."""
        require_api_key(required="admin")
        cfg = current_app.config
        client, pool_id = _require_pool(cfg)
        _get_user_or_404(client, pool_id, username)
        client.admin_add_user_to_group(
            UserPoolId=pool_id, Username=username,
            GroupName=cfg.get("AUTH_GROUP_ADMIN", "spec-admins"),
        )
        return ""


@blp.route("/users/<username>/demote")
class DemoteUser(MethodView):
    @blp.response(204)
    def post(self, username):
        """Demote an admin (remove spec-admins). Refuses self-demote and refuses
        to remove the LAST remaining admin (never leave the pool admin-less)."""
        require_api_key(required="admin")
        cfg = current_app.config
        client, pool_id = _require_pool(cfg)
        _require_self_guard_auth(cfg)
        target = _get_user_or_404(client, pool_id, username)
        admin_group = cfg.get("AUTH_GROUP_ADMIN", "spec-admins")
        if _caller_is_target(username, target):
            abort(409, message="Refusing to demote yourself — that risks locking all admins out.")
        admins = _admin_usernames(client, pool_id, admin_group)
        if username in admins and len(admins) <= 1:
            abort(409, message="Refusing to demote the last remaining admin.")
        client.admin_remove_user_from_group(
            UserPoolId=pool_id, Username=username, GroupName=admin_group
        )
        return ""


# =========================================================================== #
# HA-7 — Public request->approve signup queue: the ADMIN bridge.
#
# All endpoints are spec-admins-gated (require_api_key(required="admin")). They
# read the dedicated ${name_prefix}-signups DynamoDB table (via signup_aws, the
# same isolated-boto3 pattern as _invites_table). When SIGNUPS_TABLE is unset
# (local-dev default) every endpoint returns 501 gracefully, mirroring invites.
#
# State machine (app/signup.py): a partial (`requested`, unvalidated) row is
# listed and MAY be rejected, but APPROVE is valid ONLY from `email-validated`
# (Confirmed Decision 3). On approve the row transitions email-validated ->
# admin-approved and is PROVISIONED synchronously in the same request: an HA-2
# invite is minted (approved=true + email-bound) and the join link is SES-emailed
# to the requester, then the row is stamped `provisioned` (idempotent).
# =========================================================================== #
_MAX_REJECT_REASON_LEN = 200


def _signups_table(cfg):
    """Return the boto3 signups Table, or None. Isolated (like _invites_table) so
    tests monkeypatch it with an in-memory fake."""
    return signup_aws.signups_table(cfg)


def _require_signups_table(cfg):
    table = _signups_table(cfg)
    if table is None:
        abort(
            501,
            message=(
                "The signup queue is not configured on this server "
                "(set SIGNUPS_TABLE to the signups DynamoDB table)."
            ),
        )
    return table


def _signup_view(item: dict) -> dict:
    """Shape a signups row for AdminSignupOut. Admins may see the plaintext email
    (an SSE-KMS attribute value) to decide; keys/logs stay hashed-only."""
    def _int(v):
        return int(v) if v is not None else None
    return {
        "email_hash": item.get("email_hash"),
        "email": item.get("email"),
        "display_name": item.get("display_name") or None,
        "status": item.get("status"),
        "created_at": _int(item.get("created_at")),
        "updated_at": _int(item.get("updated_at")),
        "validated_at": _int(item.get("validated_at")),
        "approved_at": _int(item.get("approved_at")),
        "approved_by": item.get("approved_by"),
        "rejected_by": item.get("rejected_by"),
        "reject_reason": item.get("reject_reason"),
        "provisioned_at": _int(item.get("provisioned_at")),
        "resend_count": _int(item.get("resend_count")),
    }


def _get_signup(table, email_hash: str):
    resp = table.get_item(
        Key={"pk": signup_lib.signup_pk(email_hash), "sk": signup_lib.PROFILE_SK}
    )
    return resp.get("Item")


def _list_signups(table, status: str | None, limit: int = 200) -> list[dict]:
    """Query the status GSI (facet==filter: the status predicate IS the query),
    newest-first, bounded to ``limit`` rows PER state. Omitted status -> every
    state merged."""
    from boto3.dynamodb.conditions import Key

    states = [status] if status else list(signup_lib.VALID_STATES)
    items: list[dict] = []
    for st in states:
        resp = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq(signup_lib.status_gsi_pk(st)),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        items.extend(resp.get("Items", []))
    items.sort(key=lambda it: int(it.get("gsi1sk", 0) or 0), reverse=True)
    return items


def _provision_signup(cfg, row: dict, actor: str) -> None:
    """Synchronous provisioning: mint an approved, email-bound HA-2 invite and SES
    the join link, then stamp the signups row `provisioned` (exactly-once).

    Ordering: mint+email BEFORE the terminal stamp, so a failure leaves the row
    `admin-approved` (a retry re-mints a harmless second invite) rather than
    marking it provisioned without a redeemable invite. If the invites table is
    unconfigured the row stays `admin-approved` (provisioning skipped, logged)."""
    email = row.get("email")
    email_hash = row.get("email_hash")
    if not email or not email_hash:
        return
    invites = _invites_table(cfg)
    if invites is None:
        current_app.logger.warning(
            "signup_admin: INVITES_TABLE unset; approved row left un-provisioned eh=%s",
            email_hash,
        )
        return

    # Mint an approved + email-bound single-use invite (mirrors InvitesCollection
    # .post's item shape). Only the code HASH is stored; the plaintext code rides
    # only in the join link we email to the requester.
    code = secrets.token_urlsafe(_CODE_BYTES)
    now = int(time.time())
    ttl_days = int(cfg.get("INVITE_TTL_DAYS", 14))
    invites.put_item(
        Item={
            "code_hash": _hash(code),
            "status": "active",
            "created_at": now,
            "expires_at": now + ttl_days * 86400,
            "approved": True,
            "email_binding": _hash(_norm_email(email)),
        },
        ConditionExpression="attribute_not_exists(code_hash)",
    )
    base = (cfg.get("INVITE_JOIN_BASE_URL") or "").rstrip("/")
    join_url = f"{base}/join?code={code}" if base else f"/join?code={code}"

    body = (
        "Good news — your Spec Server signup has been approved.\n\n"
        "Complete your account by opening this single-use link:\n\n"
        f"{join_url}\n\n"
        "The link expires soon. If you did not request an account you can ignore "
        "this email.\n"
    )
    try:
        signup_aws.ses_send(
            cfg, to_addr=email,
            subject="Your Spec Server signup is approved", body=body,
        )
    except Exception:  # noqa: BLE001 — a send failure must not double-provision
        current_app.logger.exception("signup_admin: join-link email failed eh=%s", email_hash)
        raise

    # Stamp provisioned exactly-once (conditional guard). False = a concurrent
    # approve already stamped it — the invite we minted is a harmless duplicate.
    signup_lib.mark_provisioned(_signups_table(cfg), email_hash)


@blp.route("/signups")
class SignupsCollection(MethodView):
    @blp.arguments(AdminSignupsQuery, location="query")
    @blp.response(200, AdminSignupOut(many=True))
    def get(self, query):
        """List signup requests (every state, or one via ?status=), newest first.
        Admin-only. Partial (unvalidated) rows are first-class + listed."""
        require_api_key(required="admin")
        cfg = current_app.config
        table = _require_signups_table(cfg)
        rows = _list_signups(table, query.get("status"), query.get("limit", 200))
        return [_signup_view(it) for it in rows]


@blp.route("/signups/<email_hash>/approve")
class ApproveSignup(MethodView):
    @blp.response(200, AdminSignupOut)
    def post(self, email_hash):
        """Approve a signup — ONLY valid from email-validated — then provision
        synchronously (mint an approved+email-bound invite + SES the join link).
        Idempotent: re-approving an already-approved/provisioned row is a no-op."""
        require_api_key(required="admin")
        cfg = current_app.config
        table = _require_signups_table(cfg)
        actor = (current_identity() or {}).get("username") or (current_identity() or {}).get("sub") or "admin"

        item = _get_signup(table, email_hash)
        if not item:
            abort(404, message="signup not found")
        status = item.get("status")

        # Idempotent: already approved-or-further along -> return current row.
        if status in (signup_lib.STATE_ADMIN_APPROVED, signup_lib.STATE_PROVISIONED):
            if status == signup_lib.STATE_ADMIN_APPROVED:
                # A prior approve may have failed to provision — try again.
                _provision_signup(cfg, item, actor)
                item = _get_signup(table, email_hash) or item
            return _signup_view(item)

        if not signup_lib.can_approve(status):
            abort(409, message="approve requires the signup be email-validated first")

        now = int(time.time())
        moved = signup_lib.transition_signup(
            table, email_hash,
            from_state=signup_lib.STATE_EMAIL_VALIDATED,
            to_state=signup_lib.STATE_ADMIN_APPROVED,
            extra_set={"approved_by": actor, "approved_at": now},
            now=now,
        )
        refreshed = _get_signup(table, email_hash) or item
        if not moved:
            # Lost a race to a concurrent admin action; respond off the real row.
            if refreshed.get("status") in (
                signup_lib.STATE_ADMIN_APPROVED, signup_lib.STATE_PROVISIONED
            ):
                return _signup_view(refreshed)
            abort(409, message="conflict")

        _provision_signup(cfg, refreshed, actor)
        return _signup_view(_get_signup(table, email_hash) or refreshed)


@blp.route("/signups/<email_hash>/reject")
class RejectSignup(MethodView):
    @blp.arguments(AdminRejectIn)
    @blp.response(200, AdminSignupOut)
    def post(self, data, email_hash):
        """Reject a signup from any non-terminal state (incl. a partial
        `requested` row). Idempotent: re-rejecting is a no-op."""
        require_api_key(required="admin")
        cfg = current_app.config
        table = _require_signups_table(cfg)
        actor = (current_identity() or {}).get("username") or (current_identity() or {}).get("sub") or "admin"
        reason = str(data.get("reason") or "").strip()[:_MAX_REJECT_REASON_LEN]

        item = _get_signup(table, email_hash)
        if not item:
            abort(404, message="signup not found")
        status = item.get("status")
        if status == signup_lib.STATE_REJECTED:
            return _signup_view(item)
        if not signup_lib.can_reject(status):
            abort(409, message="signup is not in a rejectable state")

        signup_lib.reject_signup(table, email_hash, actor=actor, reason=reason)
        return _signup_view(_get_signup(table, email_hash) or item)
