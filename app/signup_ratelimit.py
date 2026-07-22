"""Per-IP fixed-window DynamoDB rate limiter for the public signup routes (HA-7).

A small, atomic fixed-window counter mirroring the bird ``common.ratelimit``
primitive. Keyed on the SOURCE IP ONLY (never the email), so it adds uniform
latency across all email values and is NOT an enumeration oracle. It complements
(does not replace) any edge/CDN throttle.

FAILS OPEN: a missing/unconfigured counter table or ANY DynamoDB error returns
"not limited" (availability over strict limiting) — the durable edge limiter is
the real backstop; this is the best-effort in-app floor.

Purity: :func:`fixed_window_incr` takes a boto3 ``Table``, so unit tests drive it
with an in-memory fake; :func:`rate_limited` resolves the table from app config.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger(__name__)

# Process-cached boto3 Table, keyed by table name; tests may inject a fake via
# ``set_table_factory``.
_TABLE_CACHE: dict = {}
_TABLE_FACTORY = None


def set_table_factory(factory) -> None:
    """Test/DI hook: ``factory(cfg) -> table_or_None`` overrides table resolution."""
    global _TABLE_FACTORY
    _TABLE_FACTORY = factory
    _TABLE_CACHE.clear()


def _default_table(cfg):
    name = cfg.get("SIGNUP_RATELIMIT_TABLE")
    if not name:
        return None
    if name in _TABLE_CACHE:
        return _TABLE_CACHE[name]
    import boto3  # lazy: keep boto3 off the import path when the limiter is unused

    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    if cfg.get("DYNAMODB_ENDPOINT_URL"):
        kwargs["endpoint_url"] = cfg["DYNAMODB_ENDPOINT_URL"]
    table = boto3.resource("dynamodb", **kwargs).Table(name)
    _TABLE_CACHE[name] = table
    return table


def _resolve_table(cfg):
    if _TABLE_FACTORY is not None:
        return _TABLE_FACTORY(cfg)
    return _default_table(cfg)


def fixed_window_incr(table, key: str, window_s: int, *, now: Optional[int] = None) -> int:
    """Atomically increment and return the counter for ``key`` in the current
    fixed window. One ``UpdateItem`` with ``ADD count :one`` returns the new
    value; a fresh ``ttl`` (2 windows out) lets DynamoDB GC the bucket."""
    now = int(time.time()) if now is None else int(now)
    window = now // int(window_s)
    pk = f"{key}#{window}"
    resp = table.update_item(
        Key={"pk": pk},
        UpdateExpression="ADD #c :one SET #ttl = :ttl",
        ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
        ExpressionAttributeValues={":one": 1, ":ttl": now + int(window_s) * 2},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp.get("Attributes", {}).get("count", 0))


def rate_limited(cfg, ip: str, *, key_prefix: str = "sig#ip#") -> bool:
    """Return True iff ``ip`` is OVER its per-window budget for a signup route.

    Email-independent (keyed on the source IP only). ``key_prefix`` keeps the
    intake (``sig#ip#``) and validate (``val#ip#``) budgets independent so a
    magic-link click never eats a submission's allowance. FAILS OPEN (returns
    False) on a missing/unavailable counter store or any DynamoDB error."""
    if not ip:
        return False
    try:
        table = _resolve_table(cfg)
        if table is None:
            return False  # limiter disabled -> fail open
        max_attempts = int(cfg.get("SIGNUP_RATELIMIT_MAX", 5))
        window_s = int(cfg.get("SIGNUP_RATELIMIT_WINDOW_S", 60))
        count = fixed_window_incr(table, f"{key_prefix}{ip}", window_s)
        if count > max_attempts:
            _log.warning("signup: rate limited source IP (%d>%d)", count, max_attempts)
            return True
    except Exception:  # noqa: BLE001 — FAIL OPEN on any DDB/infra error
        _log.warning("signup: rate-limit store unavailable; failing open", exc_info=True)
        return False
    return False
