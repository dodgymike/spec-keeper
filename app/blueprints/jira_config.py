"""Per-project Jira integration config CRUD (JIRA-5), via the storage port.

SLS-J3: all reads/writes go through ``current_app.storage`` so Jira config works
on BOTH backends (Postgres + DynamoDB) with identical observable behaviour.

Crypto boundary (single place): the blueprint ``encrypt()``s the plaintext token
before handing the storage layer the ciphertext (``api_token_encrypted``). The
plaintext token NEVER enters the storage layer, is never persisted in the clear,
and is never logged. Storage only ever sees / returns ciphertext, and responses
expose only ``has_token`` — never the token or its ciphertext (``_config_to_out``).

Transition-cache warmup on create/enable (the old JIRA-6 eager warmup) is wired
here in SLS-J4 via ``warm_transition_cache``, which persists through the
``set_jira_transitions`` storage port method so it works on BOTH backends. It is
best-effort: a warmup failure NEVER blocks the config save (the cache is then
populated lazily on first sync use via ``find_transition``'s refresh-once).
"""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..crypto import encrypt
from ..helpers import require_project_perm
from ..jira_transitions import warm_transition_cache
from ..schemas import JiraConfigIn, JiraConfigOut, JiraConfigUpdate

blp = Blueprint(
    "jira_config", __name__, url_prefix="/api/v1/projects",
    description="Per-project Jira integration configuration.",
)


def _config_to_out(config) -> dict:
    """Build the output dict — never includes the token or its ciphertext.

    ``config`` is a backend-neutral ``JiraConfigDTO``; ``has_token`` is derived
    from the presence of the ciphertext, which itself is never surfaced."""
    return {
        "base_url": config.base_url,
        "email": config.email,
        "jira_project_key": config.jira_project_key,
        "enabled": config.enabled,
        "has_token": config.api_token_encrypted is not None,
        "updated_at": config.updated_at,
    }


def _maybe_warm_transition_cache(config, slug) -> None:
    """Eagerly warm the transition cache when the config is enabled (SLS-J4).

    Best-effort: warmup failures MUST NOT block the config save, so every error
    (Jira unreachable, missing token, etc.) is swallowed and logged — the cache
    is then populated lazily on first sync use."""
    if not config.enabled:
        return
    try:
        warm_transition_cache(config, slug)
    except Exception:  # noqa: BLE001 - best-effort; never block the save
        current_app.logger.warning(
            "Jira transition-cache warmup failed for project %s; config saved, "
            "cache will populate lazily on first sync.", slug
        )


@blp.route("/<slug>/jira-config")
class JiraConfigResource(MethodView):
    @blp.response(200, JiraConfigOut)
    def get(self, slug):
        """Get the Jira integration config for a project."""
        # SEC-FIX-1: per-project authorization (NOT the bare global-group gate) —
        # otherwise any enrolled agent could read ANOTHER project's Jira config
        # under PROJECT_ISOLATION_ENFORCED. A non-member read is hidden (404).
        # SEC: consider tightening to "admin" (this resource stores integration
        # credentials); shipping "write"/"read" now for parity with the sibling
        # jira_sync_retry gate and to unblock legitimate writers.
        require_project_perm(slug, "read")
        config = current_app.storage.get_jira_config(slug)  # 404 if project absent
        if config is None:
            abort(404, message="Jira config not found for this project.")
        return _config_to_out(config)

    @blp.arguments(JiraConfigIn)
    @blp.response(201, JiraConfigOut)
    def post(self, data, slug):
        """Create Jira integration config for a project."""
        # SEC-FIX-1: per-project write authorization (see the GET note above; a
        # non-member write is 403). SEC: consider tightening to "admin".
        require_project_perm(slug, "write")
        # Encrypt HERE so storage only ever receives ciphertext.
        stored = {
            "base_url": data["base_url"],
            "email": data["email"],
            "api_token_encrypted": encrypt(data["api_token"]),
            "jira_project_key": data["jira_project_key"],
            "enabled": data.get("enabled", False),
        }
        # 404 if project absent, 409 if a config already exists.
        config = current_app.storage.create_jira_config(slug, stored)
        _maybe_warm_transition_cache(config, slug)
        return _config_to_out(config)

    @blp.arguments(JiraConfigUpdate)
    @blp.response(200, JiraConfigOut)
    def put(self, data, slug):
        """Update Jira integration config for a project."""
        # SEC-FIX-1: per-project write authorization (see the GET note above; a
        # non-member write is 403). SEC: consider tightening to "admin".
        require_project_perm(slug, "write")
        stored: dict = {}
        for fld in ("base_url", "email", "jira_project_key", "enabled"):
            if fld in data:
                stored[fld] = data[fld]
        if "api_token" in data:
            # Re-encrypt HERE; the storage layer never sees the plaintext.
            stored["api_token_encrypted"] = encrypt(data["api_token"])
        # 404 if project or config absent.
        config = current_app.storage.update_jira_config(slug, stored)
        _maybe_warm_transition_cache(config, slug)
        return _config_to_out(config)
