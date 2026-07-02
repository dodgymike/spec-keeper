"""Jira transition cache warmup and lookup (JIRA-6).

Provides helpers to:
1. Warm the transition cache: fetch available statuses/transitions from Jira
   and store them in jira_project_config.cached_transitions (JSONB).
2. Find a transition by name with refresh-once-before-failing semantics:
   if the named transition is not in the cache, refresh the cache exactly once,
   then look again. If still missing, raise an error.

The discovery mechanism uses Jira's project statuses endpoint
(GET /rest/api/3/project/{projectKey}/statuses) which returns all statuses
for all issue types in the project — this is better than per-issue transitions
because it does not require a specific issue key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from .crypto import decrypt
from .extensions import db
from .jira_client import JiraClient, JiraClientError
from .models import JiraProjectConfig

logger = logging.getLogger(__name__)


class TransitionCacheError(Exception):
    """Raised when transition cache operations fail."""


class TransitionNotFoundError(Exception):
    """Raised when a requested transition name is not in the cache,
    even after a refresh attempt."""


def _build_client(config: JiraProjectConfig) -> JiraClient:
    """Construct a JiraClient from a JiraProjectConfig row."""
    token = decrypt(config.api_token_encrypted)
    return JiraClient(
        base_url=config.base_url,
        email=config.email,
        api_token=token,
    )


def _fetch_project_statuses(client: JiraClient, project_key: str) -> list[dict]:
    """Fetch all statuses for a Jira project via the project statuses endpoint.

    Returns a flat list of unique status dicts: [{"id": "...", "name": "..."}, ...]
    """
    resp = client._request("GET", f"/project/{project_key}/statuses")
    data = resp.json()
    # The response is a list of issue types, each with a "statuses" array.
    # We flatten and deduplicate by status id.
    seen_ids: set[str] = set()
    statuses: list[dict] = []
    for issue_type_block in data:
        for status in issue_type_block.get("statuses", []):
            sid = str(status["id"])
            if sid not in seen_ids:
                seen_ids.add(sid)
                statuses.append({"id": sid, "name": status["name"]})
    return statuses


def warm_transition_cache(config: JiraProjectConfig) -> list[dict]:
    """Fetch project statuses from Jira and store in cached_transitions.

    Args:
        config: A JiraProjectConfig row (must have api_token_encrypted set).

    Returns:
        The list of statuses that was cached.

    Raises:
        TransitionCacheError: If the Jira API call fails.
    """
    if not config.api_token_encrypted:
        raise TransitionCacheError(
            "Cannot warm transition cache: no API token configured."
        )

    client = _build_client(config)
    try:
        statuses = _fetch_project_statuses(client, config.jira_project_key)
    except JiraClientError as exc:
        logger.warning(
            "Failed to warm transition cache for project_key=%s: %s",
            config.jira_project_key,
            exc,
        )
        raise TransitionCacheError(
            f"Jira API call failed (HTTP {exc.status_code})"
        ) from exc

    config.cached_transitions = {
        "statuses": statuses,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    db.session.commit()
    return statuses


def find_transition(
    config: JiraProjectConfig,
    transition_name: str,
    *,
    allow_refresh: bool = True,
) -> dict:
    """Find a transition/status by name in the cache, refreshing once if needed.

    Implements the refresh-once-before-failing pattern:
    1. Look in the existing cache.
    2. If not found and allow_refresh is True, refresh the cache from Jira.
    3. Look again. If still not found, raise TransitionNotFoundError.

    Args:
        config: The JiraProjectConfig row.
        transition_name: The name to search for (case-insensitive match).
        allow_refresh: If True (default), refresh cache once on miss.

    Returns:
        The matching status dict {"id": "...", "name": "..."}.

    Raises:
        TransitionNotFoundError: If the name is not found even after refresh.
        TransitionCacheError: If the refresh itself fails.
    """
    # First attempt: look in existing cache
    match = _find_in_cache(config, transition_name)
    if match is not None:
        return match

    # Cache miss — refresh once if allowed
    if not allow_refresh:
        raise TransitionNotFoundError(
            f"Transition '{transition_name}' not found in cache "
            f"(refresh disabled)."
        )

    logger.info(
        "Transition '%s' not in cache for project_key=%s; refreshing.",
        transition_name,
        config.jira_project_key,
    )
    warm_transition_cache(config)

    # Second attempt after refresh
    match = _find_in_cache(config, transition_name)
    if match is not None:
        return match

    raise TransitionNotFoundError(
        f"Transition '{transition_name}' not found in cache for "
        f"project '{config.jira_project_key}', even after refresh."
    )


def _find_in_cache(config: JiraProjectConfig, name: str) -> dict | None:
    """Search cached_transitions for a status matching `name` (case-insensitive)."""
    if not config.cached_transitions:
        return None
    statuses = config.cached_transitions.get("statuses", [])
    name_lower = name.lower()
    for status in statuses:
        if status.get("name", "").lower() == name_lower:
            return status
    return None
