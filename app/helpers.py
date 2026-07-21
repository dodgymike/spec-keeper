"""Shared request helpers: lookups, optimistic-lock checks, auth.

Auth precedence ladder (AUTH-2), evaluated per request in ``require_api_key``:

1. ``COGNITO_ISSUER`` configured -> require & validate a Cognito RS256 JWT and
   enforce the per-method/-resource scope (401 on missing/invalid/expired token,
   403 on a valid token lacking the required scope).
2. else ``API_KEYS`` configured -> the legacy static bearer-token check.
3. else -> open (the local-only default; behaviour unchanged).

Scope mapping (HTTP method + blueprint -> required custom scope name):
    GET / HEAD (any resource)                         -> tasks.read
    mutations on the projects / agents blueprints     -> projects.admin
    all other mutations (tasks, epics, reservations,  -> tasks.write
        ports, log, chains)

The JWKS is fetched once and cached (TTL + refresh-on-unknown-kid) so a burst of
requests cannot turn into a burst of outbound JWKS fetches. Signatures are pinned
to RS256, defeating ``alg=none``/HS-with-the-RSA-key confusion attacks.
"""
from __future__ import annotations

import threading
import time

import sqlalchemy as sa
from flask import current_app, request
from flask_smorest import abort

from .extensions import db
from .models import Epic, Project, Task

#: Blueprints whose *mutations* are administrative (project/agent management).
_ADMIN_BLUEPRINTS = frozenset({"projects", "agents"})


# --------------------------------------------------------------------------- #
# Auth entry point + precedence ladder
# --------------------------------------------------------------------------- #
def require_api_key() -> None:
    """Enforce auth per the precedence ladder documented in the module header.

    Called (with no arguments) at the top of every blueprint handler; it derives
    the required scope from the live ``request`` (method + blueprint)."""
    cfg = current_app.config
    issuer = cfg.get("COGNITO_ISSUER")
    if issuer:
        _require_cognito_jwt(cfg, issuer)
        return

    keys = cfg.get("API_KEYS") or []
    if not keys:
        return
    token = _bearer_token()
    if token not in keys:
        abort(401, message="Missing or invalid bearer token.")


def _bearer_token() -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return None


def _require_cognito_jwt(cfg, issuer: str) -> None:
    token = _bearer_token()
    if not token:
        abort(401, message="Missing bearer token.")
    claims = _decode_and_verify(token, cfg, issuer)
    required = _required_scope(cfg)
    if required and not _scope_satisfied(required, _token_scopes(claims)):
        abort(403, message=f"Token lacks required scope '{required}'.")


def _required_scope(cfg) -> str:
    """Map the current request (method + blueprint) to a required scope name."""
    method = request.method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return cfg.get("AUTH_SCOPE_READ", "tasks.read")
    if request.blueprint in _ADMIN_BLUEPRINTS:
        return cfg.get("AUTH_SCOPE_ADMIN", "projects.admin")
    return cfg.get("AUTH_SCOPE_WRITE", "tasks.write")


def _token_scopes(claims: dict) -> set[str]:
    """Collect granted scopes from the ``scope`` (space-delimited) / ``scp`` claim."""
    raw = claims.get("scope")
    if raw is None:
        raw = claims.get("scp")
    if isinstance(raw, str):
        return set(raw.split())
    if isinstance(raw, (list, tuple)):
        return {str(s) for s in raw}
    return set()


def _scope_satisfied(required: str, granted: set[str]) -> bool:
    """A required scope *name* is satisfied by a granted scope that is either the
    bare name or a full ``<resource-server>/<name>`` identifier."""
    for g in granted:
        if g == required or g.rsplit("/", 1)[-1] == required:
            return True
    return False


# --------------------------------------------------------------------------- #
# JWT verification (RS256, pinned) + audience / token_use checks
# --------------------------------------------------------------------------- #
def _default_jwks_uri(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/jwks.json"


def _decode_and_verify(token: str, cfg, issuer: str) -> dict:
    """Verify signature (RS256 only), issuer, exp/nbf, then audience + token_use.

    Raises (via ``abort``) 401 on any failure. Never logs the token."""
    import jwt  # lazy: keeps the import optional when auth is off

    jwks_uri = cfg.get("COGNITO_JWKS_URI") or _default_jwks_uri(issuer)
    ttl = cfg.get("JWKS_CACHE_TTL", 3600)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        abort(401, message="Malformed authentication token.")
    kid = header.get("kid")
    if not kid:
        abort(401, message="Authentication token missing key id.")

    min_refresh = cfg.get("JWKS_MIN_REFRESH_INTERVAL", 30)
    public_key = _get_jwks_cache(jwks_uri, ttl, min_refresh).public_key(kid)
    if public_key is None:
        abort(401, message="Unknown token signing key.")

    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],  # pin RS256: rejects alg=none / HS confusion
            issuer=issuer,
            leeway=cfg.get("AUTH_LEEWAY", 0),
            options={"verify_aud": False, "require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        abort(401, message="Invalid or expired authentication token.")

    _check_audience(claims, cfg)
    _check_token_use(claims, cfg)
    return claims


def _check_audience(claims: dict, cfg) -> None:
    allowed = cfg.get("COGNITO_AUDIENCE") or []
    if not allowed:
        return
    candidates: set[str] = set()
    aud = claims.get("aud")
    if isinstance(aud, str):
        candidates.add(aud)
    elif isinstance(aud, (list, tuple)):
        candidates.update(str(a) for a in aud)
    if claims.get("client_id"):
        candidates.add(str(claims["client_id"]))
    if candidates.isdisjoint(allowed):
        abort(401, message="Token audience is not allowed.")


def _check_token_use(claims: dict, cfg) -> None:
    expected = cfg.get("COGNITO_TOKEN_USE")
    if not expected:
        return
    if claims.get("token_use") != expected:
        abort(401, message="Unexpected token_use claim.")


# --------------------------------------------------------------------------- #
# Cached JWKS (TTL + refresh-on-unknown-kid). Keyed by URI so distinct app
# configs (and tests) keep independent caches; a hot cache serves requests
# without any outbound fetch (guards against a JWKS-fetch DoS).
# --------------------------------------------------------------------------- #
def _http_get_json(uri: str) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(uri, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (https URI)
        return json.loads(resp.read().decode("utf-8"))


class _JWKSCache:
    def __init__(self, uri: str, ttl: float, min_refresh: float = 30.0) -> None:
        self.uri = uri
        self.ttl = ttl
        # Minimum seconds between refetches driven by an *unknown kid* (as
        # opposed to TTL expiry). Bounds a "flood of bogus kids" into at most
        # one outbound JWKS fetch per interval, so a caller cannot amplify
        # unauthenticated requests into a JWKS-endpoint DoS. Key rotation is
        # still picked up within this window (and immediately on TTL expiry).
        self.min_refresh = min_refresh
        self._keys: dict[str, dict] = {}
        self._fetched = 0.0
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def _load(self) -> None:
        data = _http_get_json(self.uri)
        self._keys = {
            jwk["kid"]: jwk for jwk in data.get("keys", []) if jwk.get("kid")
        }
        self._fetched = time.monotonic()

    def public_key(self, kid: str):
        import jwt

        with self._lock:
            now = time.monotonic()
            cold = not self._keys
            expired = (now - self._fetched) > self.ttl
            unknown_kid = kid not in self._keys
            cooldown_ok = (now - self._last_attempt) >= self.min_refresh
            # Always (re)load when cold or past TTL; for an unknown kid on an
            # otherwise-fresh cache, only refetch if the cooldown has elapsed.
            if cold or expired or (unknown_kid and cooldown_ok):
                self._last_attempt = now
                try:
                    self._load()
                except Exception:  # keep serving stale keys on a fetch blip
                    pass
            jwk = self._keys.get(kid)
            if jwk is None:
                return None
            return jwt.PyJWK.from_dict(jwk).key


_JWKS_CACHES: dict[str, _JWKSCache] = {}
_JWKS_CACHES_LOCK = threading.Lock()


def _get_jwks_cache(uri: str, ttl: float, min_refresh: float = 30.0) -> _JWKSCache:
    with _JWKS_CACHES_LOCK:
        cache = _JWKS_CACHES.get(uri)
        if cache is None or cache.ttl != ttl or cache.min_refresh != min_refresh:
            cache = _JWKSCache(uri, ttl, min_refresh)
            _JWKS_CACHES[uri] = cache
        return cache


def _reset_jwks_cache() -> None:
    """Test hook: drop all cached JWKS."""
    with _JWKS_CACHES_LOCK:
        _JWKS_CACHES.clear()


def get_project_or_404(slug: str) -> Project:
    project = db.session.execute(
        sa.select(Project).where(Project.slug == slug)
    ).scalar_one_or_none()
    if project is None:
        abort(404, message=f"Project '{slug}' not found.")
    return project


def get_epic_or_404(project_id: int, key: str) -> Epic:
    epic = db.session.execute(
        sa.select(Epic).where(Epic.project_id == project_id, Epic.key == key)
    ).scalar_one_or_none()
    if epic is None:
        abort(404, message=f"Epic '{key}' not found.")
    return epic


def get_task_or_404(project_id: int, ident: str) -> Task:
    """Look up a task by human key first, then public_id."""
    task = db.session.execute(
        sa.select(Task).where(Task.project_id == project_id, Task.key == ident)
    ).scalar_one_or_none()
    if task is None:
        task = db.session.execute(
            sa.select(Task).where(
                Task.project_id == project_id, Task.public_id == ident
            )
        ).scalar_one_or_none()
    if task is None:
        abort(404, message=f"Task '{ident}' not found.")
    return task


def check_if_match(task: Task) -> None:
    """Enforce optimistic locking. If the client sent If-Match it must equal
    the current task version, otherwise 412. If absent, the write proceeds
    (lenient for non-concurrent callers), matching simple agent usage."""
    if_match = request.headers.get("If-Match")
    if if_match is None:
        return
    expected = if_match.strip().strip('"').lstrip("v")
    if str(task.version) != expected:
        abort(
            412,
            message=(
                f"Version conflict: task is at v{task.version}, "
                f"you sent If-Match {if_match!r}. Re-read and retry."
            ),
        )


def etag_headers(task) -> dict:
    """Build the ETag header from anything carrying a ``version`` (ORM or DTO)."""
    return {"ETag": f'"v{task.version}"'}


def expected_version_from_request() -> str | None:
    """Parse the ``If-Match`` request header into a bare version token.

    Returns the value with surrounding quotes and a leading ``v`` stripped
    (e.g. ``'"v3"'`` -> ``'3'``), or ``None`` when the header is absent. The
    storage layer compares this against the task's current version and raises
    ``VersionConflict`` (-> 412) on mismatch — preserving the old lenient
    behaviour where a missing header skips the check.
    """
    if_match = request.headers.get("If-Match")
    if if_match is None:
        return None
    return if_match.strip().strip('"').lstrip("v")
