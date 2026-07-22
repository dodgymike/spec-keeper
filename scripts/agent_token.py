#!/usr/bin/env python3
"""Cognito USER-auth access-token helper for agents calling the deployed Spec Server.

The M2M ``client_credentials`` clients were retired (AUTH-10); agents now sign in
as Cognito **users** against the ``agents`` app client (no client secret) using the
``USER_PASSWORD_AUTH`` flow. This helper performs that ``InitiateAuth``, caches the
resulting **access token in memory**, transparently **refreshes** it via
``REFRESH_TOKEN_AUTH`` shortly before expiry, and re-authenticates from scratch when
the server answers a call with **401**. Drop ``TokenProvider`` / ``authorized_request``
into an agent and every API call carries a fresh ``Authorization: Bearer <access>``.

Authorization on the server is by Cognito **group** membership (``cognito:groups``):
``spec-admins`` => read+write+admin, ``spec-writers`` => read+write, ``spec-readers``
=> read. Put the agent user in the group matching the calls it makes.

Locally the Spec Server runs with auth OFF (no ``COGNITO_ISSUER``); you need none of
this. It matters only against a deployed server where a Cognito issuer is set.

Configuration (env), two ways to supply the user credentials:

  1. Secrets Manager (recommended) — the ``agent-credentials`` secret written by
     infra holds everything as JSON::

         {"pool_id": "...", "client_id": "...", "region": "us-east-1",
          "users": {"planner": {"password": "...", "groups": ["spec-writers"]}, ...}}

     Point ``AGENT_CREDENTIALS_SECRET_ARN`` (or ``AGENT_CREDENTIALS_SECRET``) at it
     and select the user with ``AGENT_USERNAME`` (omit if the secret has exactly one
     user). Needs boto3 + ``secretsmanager:GetSecretValue`` on THAT secret only.

  2. Inline (dev / CI)::

         AGENT_USERNAME       the Cognito username
         AGENT_PASSWORD       its password
         COGNITO_CLIENT_ID    the `agents` app-client id (no secret)
         COGNITO_REGION       AWS region of the user pool

  Env values, if present, override individual fields resolved from the secret.

Secret-safety: the password, access token, and refresh token are held only in
memory and are NEVER printed, logged, or included in any exception message. ``repr``
of the provider shows no material. Run as a script it prints only the token's
*expiry*, never any token. ``InitiateAuth`` is an unauthenticated Cognito API, so
the boto3 client is created UNSIGNED — no AWS credentials are needed or used.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

# Refresh this many seconds BEFORE the stated expiry, so an in-flight request
# never races the clock.
_EXPIRY_SKEW = 60.0


class TokenError(RuntimeError):
    """Auth failed. Message is safe to log (carries no secret/password/token)."""


def _load_config() -> dict:
    """Resolve username/password/client_id/region from the secret and/or env."""
    cfg: dict[str, str] = {}
    secret_id = (
        os.environ.get("AGENT_CREDENTIALS_SECRET_ARN")
        or os.environ.get("AGENT_CREDENTIALS_SECRET")
    )
    # AGENT_USERNAME is the SELECTOR into the secret's `users` map (the roster
    # key, e.g. "spec-keeper"); on the inline/dev path (no secret) it is taken as
    # the literal Cognito username below.
    selector = os.environ.get("AGENT_USERNAME")
    if secret_id:
        secret = _load_from_secrets_manager(secret_id)
        for k in ("client_id", "region", "pool_id"):
            if secret.get(k):
                cfg[k] = str(secret[k])
        users = secret.get("users") or {}
        if isinstance(users, dict) and users:
            # Pick the requested user, or the sole user if the secret has one.
            if selector is None and len(users) == 1:
                selector = next(iter(users))
            if selector and selector in users:
                rec = users[selector] or {}
                # The Cognito sign-in USERNAME is the record's `username` (the
                # pool alias, e.g. "spec-keeper@agents.spec-server.internal") —
                # NOT the roster key used to select it. Fall back to the key for
                # legacy secrets written without a `username` field.
                cfg["username"] = str(rec.get("username") or selector)
                pw = rec.get("password")
                if pw:
                    cfg["password"] = str(pw)
    # Env vars win over (and fill gaps in) the secret blob.
    env_map = {
        "password": "AGENT_PASSWORD",
        "client_id": "COGNITO_CLIENT_ID",
        "region": "COGNITO_REGION",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    # Inline/dev path: with no secret-resolved user, AGENT_USERNAME is the
    # literal Cognito username.
    if not cfg.get("username") and selector:
        cfg["username"] = selector
    missing = [k for k in ("username", "password", "client_id", "region") if not cfg.get(k)]
    if missing:
        raise TokenError(
            "Missing agent auth config: "
            + ", ".join(missing)
            + ". Set AGENT_USERNAME/AGENT_PASSWORD/COGNITO_CLIENT_ID/COGNITO_REGION "
            "or point AGENT_CREDENTIALS_SECRET_ARN at the agent-credentials secret."
        )
    return cfg


def _load_from_secrets_manager(secret_id: str) -> dict:
    try:
        import boto3  # lazy: only needed for the Secrets Manager path
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise TokenError(
            "AGENT_CREDENTIALS_SECRET_ARN set but boto3 is not installed."
        ) from exc
    try:
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_id)
        data = json.loads(resp["SecretString"])
    except Exception as exc:  # noqa: BLE001 - never surface secret material
        raise TokenError(f"Could not read agent-credentials secret ({type(exc).__name__}).") from None
    if not isinstance(data, dict):
        raise TokenError("agent-credentials secret is not a JSON object.")
    return data


def _cognito_client(region: str):
    """A cognito-idp client configured UNSIGNED (InitiateAuth needs no AWS creds)."""
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise TokenError("boto3 is required for agent user auth but is not installed.") from exc
    return boto3.client(
        "cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED)
    )


class TokenProvider:
    """Thread-safe, self-refreshing source of a Cognito user access token.

    ``token()`` returns a cached access token, authenticating on first use, using
    the refresh token to renew it within ``_EXPIRY_SKEW`` of expiry, and doing a
    full re-authentication when no refresh token is available. Call ``invalidate()``
    after a 401 to force the next ``token()`` to re-authenticate from scratch.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or _load_config()
        self._lock = threading.Lock()
        self._client = None
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0

    # -- public API ------------------------------------------------------
    def token(self, *, force: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            if force:
                self._authenticate_locked()
            elif self._token is None:
                self._authenticate_locked()
            elif now >= self._expires_at:
                self._renew_locked()
            return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Drop cached tokens (call after a 401 so the next use re-authenticates)."""
        with self._lock:
            self._token = None
            self._refresh_token = None
            self._expires_at = 0.0

    # -- internals -------------------------------------------------------
    def _idp(self):
        if self._client is None:
            self._client = _cognito_client(self._config["region"])
        return self._client

    def _authenticate_locked(self) -> None:
        """Full USER_PASSWORD_AUTH: username+password -> access + refresh."""
        try:
            resp = self._idp().initiate_auth(
                AuthFlow="USER_PASSWORD_AUTH",
                ClientId=self._config["client_id"],
                AuthParameters={
                    "USERNAME": self._config["username"],
                    "PASSWORD": self._config["password"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - never surface password material
            raise TokenError(f"Cognito authentication failed ({type(exc).__name__}).") from None
        self._store_result(resp, keep_refresh_on_missing=False)

    def _renew_locked(self) -> None:
        """REFRESH_TOKEN_AUTH: swap the refresh token for a fresh access token.

        Falls back to a full re-authentication if we hold no refresh token or the
        refresh is rejected (e.g. the refresh token itself expired)."""
        if not self._refresh_token:
            self._authenticate_locked()
            return
        try:
            resp = self._idp().initiate_auth(
                AuthFlow="REFRESH_TOKEN_AUTH",
                ClientId=self._config["client_id"],
                AuthParameters={"REFRESH_TOKEN": self._refresh_token},
            )
        except Exception:  # noqa: BLE001 - refresh expired/revoked -> re-auth
            self._authenticate_locked()
            return
        self._store_result(resp, keep_refresh_on_missing=True)

    def _store_result(self, resp: dict, *, keep_refresh_on_missing: bool) -> None:
        result = resp.get("AuthenticationResult") or {}
        token = result.get("AccessToken")
        if not token:
            # A challenge (e.g. NEW_PASSWORD_REQUIRED) or malformed response.
            raise TokenError("Cognito did not return an access token (challenge required?).")
        refresh = result.get("RefreshToken")
        if refresh:
            self._refresh_token = refresh
        elif not keep_refresh_on_missing:
            self._refresh_token = None
        expires_in = float(result.get("ExpiresIn", 3600))
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, expires_in - _EXPIRY_SKEW)

    def __repr__(self) -> str:  # never leak any token/password
        state = "cached" if self._token else "empty"
        return f"<TokenProvider {state} user={self._config.get('username', '?')}>"


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
    """Return a valid access token from the shared provider (auth/refresh as needed)."""
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
    """Make an HTTP call with a Bearer token, retrying ONCE on 401 after re-auth.

    Returns ``(status_code, body_bytes)``. Use this as the drop-in for every Spec
    Server call; it keeps one cached token and re-mints only when the server says
    the current one is no longer good.
    """
    prov = provider or get_provider()
    hdrs = dict(headers or {})
    # The deployed API is fronted by Cloudflare, which bot-blocks the default
    # Python-urllib User-Agent (HTTP 403, error 1010). Send a stable client UA
    # so agents using this helper aren't fingerprinted; callers may override.
    hdrs.setdefault("User-Agent", "spec-agent/1.0")

    def _send(tok: str):
        h = dict(hdrs, Authorization=f"Bearer {tok}")
        req = urllib.request.Request(url, data=data, headers=h, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    status, body = _send(prov.token())
    if status == 401:  # token rejected — re-authenticate once and retry
        prov.invalidate()
        status, body = _send(prov.token(force=True))
    return status, body


if __name__ == "__main__":
    # Smoke check: authenticate and report ONLY the token's lifetime, never its value.
    try:
        p = TokenProvider()
        p.token()
        remaining = int(p._expires_at - time.monotonic())  # noqa: SLF001
        print(f"OK: signed in as a Cognito user (access token usable ~{remaining}s before refresh).")
    except TokenError as exc:
        raise SystemExit(f"agent_token: {exc}")
