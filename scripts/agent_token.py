#!/usr/bin/env python3
"""Cognito M2M access-token helper for agents calling the deployed Spec Server.

Performs the OAuth2 ``client_credentials`` grant against the Cognito token
endpoint, caches the resulting JWT **in memory**, and transparently refreshes it
when it is about to expire or when the server answers a call with **401**. Drop
``TokenProvider`` / ``authorized_request`` into an agent and every API call
carries a fresh ``Authorization: Bearer <jwt>`` header.

Locally the Spec Server runs with auth OFF (no ``COGNITO_ISSUER``); you need none
of this. It matters only against a deployed server where a Cognito issuer is set.

Configuration (env), two ways to supply the client credentials:

  1. Inline (dev / CI):
       AGENT_CLIENT_ID        Cognito M2M app client id
       AGENT_CLIENT_SECRET    its client secret
       AGENT_TOKEN_ENDPOINT   e.g. https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
       AGENT_SCOPES           space-separated scopes, e.g. "https://api.spec-server/tasks.read https://api.spec-server/tasks.write"

  2. Secrets Manager (recommended for anything real) — the secret written by
     infra/terraform/cognito.tf already holds everything as JSON
     ({client_id, client_secret, token_endpoint, scopes}):
       AGENT_CLIENT_SECRET_ARN   the Secrets Manager secret ARN (or name)
     Needs boto3 + secretsmanager:GetSecretValue on THAT ARN only. Env values,
     if also present, override individual fields from the secret.

Secret-safety: the client secret and the access token are held only in memory and
are NEVER printed, logged, or included in any exception message. ``repr`` of the
provider shows no material. When run as a script it prints only the token's
*expiry*, never the token itself.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Refresh this many seconds BEFORE the stated expiry, so an in-flight request
# never races the clock.
_EXPIRY_SKEW = 60.0
_TOKEN_TIMEOUT = 10  # seconds for the token-endpoint round trip


class TokenError(RuntimeError):
    """Token fetch failed. Message is safe to log (carries no secret/token)."""


def _load_config() -> dict:
    """Resolve client_id/secret/token_endpoint/scopes from env and/or Secrets Manager."""
    cfg: dict[str, str] = {}
    arn = os.environ.get("AGENT_CLIENT_SECRET_ARN")
    if arn:
        cfg.update(_load_from_secrets_manager(arn))
    # Env vars win over (and fill gaps in) the Secrets Manager blob.
    env_map = {
        "client_id": "AGENT_CLIENT_ID",
        "client_secret": "AGENT_CLIENT_SECRET",
        "token_endpoint": "AGENT_TOKEN_ENDPOINT",
        "scopes": "AGENT_SCOPES",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    missing = [k for k in ("client_id", "client_secret", "token_endpoint") if not cfg.get(k)]
    if missing:
        raise TokenError(
            "Missing agent auth config: "
            + ", ".join(missing)
            + ". Set AGENT_CLIENT_ID/AGENT_CLIENT_SECRET/AGENT_TOKEN_ENDPOINT "
            "or AGENT_CLIENT_SECRET_ARN."
        )
    if isinstance(cfg.get("scopes"), (list, tuple)):
        cfg["scopes"] = " ".join(cfg["scopes"])
    return cfg


def _load_from_secrets_manager(secret_id: str) -> dict:
    try:
        import boto3  # lazy: only needed for the Secrets Manager path
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise TokenError("AGENT_CLIENT_SECRET_ARN set but boto3 is not installed.") from exc
    try:
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_id)
        data = json.loads(resp["SecretString"])
    except Exception as exc:  # noqa: BLE001 - never surface secret material
        raise TokenError(f"Could not read agent secret ({type(exc).__name__}).") from None
    return {k: data[k] for k in ("client_id", "client_secret", "token_endpoint", "scopes") if k in data}


class TokenProvider:
    """Thread-safe, self-refreshing source of a Cognito M2M access token.

    ``token()`` returns a cached JWT, minting a new one on first use or when the
    cached one is within ``_EXPIRY_SKEW`` of expiry. Call ``invalidate()`` after a
    401 to force the next ``token()`` to re-mint.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or _load_config()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            if force or self._token is None or now >= self._expires_at:
                self._refresh_locked()
            return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Drop the cached token (call after a 401 so the next use re-mints)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _refresh_locked(self) -> None:
        body = {"grant_type": "client_credentials"}
        if self._config.get("scopes"):
            body["scope"] = self._config["scopes"]
        data = urllib.parse.urlencode(body).encode()
        creds = f"{self._config['client_id']}:{self._config['client_secret']}"
        basic = base64.b64encode(creds.encode()).decode()
        req = urllib.request.Request(
            self._config["token_endpoint"],
            data=data,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TOKEN_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # Don't echo the body: it can contain request context. Status only.
            raise TokenError(f"Token endpoint returned HTTP {exc.code}.") from None
        except Exception as exc:  # noqa: BLE001
            raise TokenError(f"Token request failed ({type(exc).__name__}).") from None
        token = payload.get("access_token")
        if not token:
            raise TokenError("Token endpoint response had no access_token.")
        expires_in = float(payload.get("expires_in", 3600))
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, expires_in - _EXPIRY_SKEW)

    def __repr__(self) -> str:  # never leak the token
        state = "cached" if self._token else "empty"
        return f"<TokenProvider {state} client_id={self._config.get('client_id', '?')}>"


# A process-wide default provider so a whole agent shares one cached token.
_DEFAULT: TokenProvider | None = None
_DEFAULT_LOCK = threading.Lock()


def get_provider() -> TokenProvider:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TokenProvider()
        return _DEFAULT


def get_token(*, force: bool = False) -> str:
    """Return a valid access token from the shared provider (mint/refresh as needed)."""
    return get_provider().token(force=force)


def authorized_request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    provider: TokenProvider | None = None,
    timeout: int = 30,
):
    """Make an HTTP call with a Bearer token, retrying ONCE on 401 after refresh.

    Returns ``(status_code, body_bytes)``. Use this as the drop-in for every Spec
    Server call; it keeps one cached token and re-mints only when the server says
    the current one is no longer good.
    """
    prov = provider or get_provider()
    hdrs = dict(headers or {})

    def _send(tok: str):
        h = dict(hdrs, Authorization=f"Bearer {tok}")
        req = urllib.request.Request(url, data=data, headers=h, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    status, body = _send(prov.token())
    if status == 401:  # token rejected — refresh once and retry
        prov.invalidate()
        status, body = _send(prov.token(force=True))
    return status, body


if __name__ == "__main__":
    # Smoke check: mint a token and report ONLY its lifetime, never its value.
    try:
        p = TokenProvider()
        p.token()
        remaining = int(p._expires_at - time.monotonic())  # noqa: SLF001
        print(f"OK: minted M2M access token (usable for ~{remaining}s before refresh).")
    except TokenError as exc:
        raise SystemExit(f"agent_token: {exc}")
