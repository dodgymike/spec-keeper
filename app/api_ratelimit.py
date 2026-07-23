"""Per-authenticated-principal (per Cognito ``sub``) API throttle (SEC-DOS-2).

The API Gateway stage limit (``apigw.tf`` ``default_route_settings``) is a single
budget SHARED across ALL tokens, so one token/tenant can starve everyone. This
adds a PER-``sub`` in-app floor: an atomic DynamoDB fixed-window counter keyed on
the VERIFIED access-token ``sub`` (NEVER a client-supplied value), enforced right
after the JWT is verified in :func:`app.helpers.require_api_key`, on the
authenticated ``/api/v1`` data-plane only. Public routes (health/docs/openapi/
signup/validate/redeem) never reach this — they carry no verified ``sub``.

Reuses the atomic fixed-window primitive (:func:`app.signup_ratelimit.fixed_window_incr`)
AND the physical signup-ratelimit DynamoDB table under a distinct ``apisub#`` key
namespace, so it needs NO new infra or IAM — the app Lambda already holds
``dynamodb:UpdateItem`` on that table (signups.tf ``AppRateLimitCounter``).

FAILS OPEN (critical — this is the authenticated hot path):
  * DISABLED unless ``API_RATELIMIT_TABLE`` is configured (unset => allow);
  * on ANY counter/DynamoDB error or timeout => allow the request.
The default cap (120 req / 10 s per sub) sits well above normal agent usage, so a
legitimate agent is never throttled; both knobs (``API_RATELIMIT_MAX`` /
``API_RATELIMIT_WINDOW_S``) are env-tunable without a redeploy. The verified
``sub`` is never logged (only the over/under-cap counts).

Purity: :func:`check_rate_limit` takes ``cfg`` and resolves a boto3 ``Table``;
tests inject an in-memory fake via :func:`set_table_factory`.
"""
from __future__ import annotations

import logging
import time

from .signup_ratelimit import fixed_window_incr

_log = logging.getLogger(__name__)

# Process-cached boto3 Table keyed by table name; tests may inject a fake via
# ``set_table_factory``.
_TABLE_CACHE: dict = {}
_TABLE_FACTORY = None


def set_table_factory(factory) -> None:
    """Test/DI hook: ``factory(cfg) -> table_or_None`` overrides table resolution."""
    global _TABLE_FACTORY
    _TABLE_FACTORY = factory
    _TABLE_CACHE.clear()


def _default_table(cfg):
    name = cfg.get("API_RATELIMIT_TABLE")
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


def check_rate_limit(cfg, sub: str, *, key_prefix: str = "apisub#") -> tuple[bool, int]:
    """Return ``(limited, retry_after_seconds)`` for the VERIFIED principal ``sub``.

    Atomically increments a per-``sub`` fixed-window counter and compares it to
    ``API_RATELIMIT_MAX`` over ``API_RATELIMIT_WINDOW_S`` seconds; ``retry_after``
    is the whole seconds left in the current window (>=1). Keyed ONLY on the
    verified ``sub`` (never client input), so one principal's traffic can never
    consume another's budget.

    FAILS OPEN — returns ``(False, 0)`` when the limiter is unconfigured, ``sub``
    is empty/None, or ANY DynamoDB/infra error occurs. A counter-backend hiccup
    must never 429 a legitimate caller."""
    if not sub:
        return False, 0
    try:
        table = _resolve_table(cfg)
        if table is None:
            return False, 0  # limiter disabled -> fail open
        max_req = int(cfg.get("API_RATELIMIT_MAX", 120))
        window_s = int(cfg.get("API_RATELIMIT_WINDOW_S", 10))
        now = int(time.time())
        count = fixed_window_incr(table, f"{key_prefix}{sub}", window_s, now=now)
        if count > max_req:
            retry_after = window_s - (now % window_s)
            _log.warning("api throttle: principal over budget (%d>%d)", count, max_req)
            return True, max(1, retry_after)
    except Exception:  # noqa: BLE001 — FAIL OPEN on any DDB/infra error
        _log.warning(
            "api throttle: counter store unavailable; failing open", exc_info=True
        )
        return False, 0
    return False, 0
