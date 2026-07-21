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

from ..helpers import require_api_key
from ..schemas import InviteIn, InviteMintOut, InviteOut

blp = Blueprint(
    "admin", __name__, url_prefix="/api/v1/admin",
    description="Admin-only operations (invite-only human signup).",
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
