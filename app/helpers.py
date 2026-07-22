"""Shared request helpers: lookups, optimistic-lock checks, auth.

Auth precedence ladder (AUTH-2, group model per AUTH-10), evaluated per request
in ``require_api_key``:

1. ``COGNITO_ISSUER`` configured -> require & validate a Cognito RS256 JWT and
   enforce the per-method/-resource permission derived from the token's Cognito
   *group* membership (401 on missing/invalid/expired token, 403 on a valid
   token whose groups do not grant the required permission).
2. else ``API_KEYS`` configured -> the legacy static bearer-token check.
3. else -> open (the local-only default; behaviour unchanged).

Group -> permission model (a user's effective permissions are the union over its
Cognito groups, read from the verified ``cognito:groups`` access-token claim):
    spec-admins  -> {read, write, admin}
    spec-writers -> {read, write}
    spec-readers -> {read}
A token carrying no known group grants no permissions (403 on anything needing
read or above).

Method + blueprint -> required permission:
    GET / HEAD (any resource)                         -> read
    mutations on the projects / agents blueprints     -> admin
    all other mutations (tasks, epics, reservations,  -> write
        ports, log, chains)

The JWKS is fetched once and cached (TTL + refresh-on-unknown-kid) so a burst of
requests cannot turn into a burst of outbound JWKS fetches. Signatures are pinned
to RS256, defeating ``alg=none``/HS-with-the-RSA-key confusion attacks.
"""
from __future__ import annotations

import threading
import time

import sqlalchemy as sa
from flask import current_app, g, request
from flask_smorest import abort

from .extensions import db
from .models import Epic, Project, Task
from .storage.errors import NotFound as _StorageNotFound

#: Blueprints whose *mutations* are administrative (project/agent management).
_ADMIN_BLUEPRINTS = frozenset({"projects", "agents"})

#: Permission tokens used by the group model (union'd across a user's groups).
_PERM_READ = "read"
_PERM_WRITE = "write"
_PERM_ADMIN = "admin"

#: Membership role -> granted permission set (ISO-4). Mirrors the global group
#: model so a project role grants the same permission tokens require_api_key uses.
_ROLE_PERMISSIONS = {
    "reader": frozenset({_PERM_READ}),
    "writer": frozenset({_PERM_READ, _PERM_WRITE}),
    "admin": frozenset({_PERM_READ, _PERM_WRITE, _PERM_ADMIN}),
}


# --------------------------------------------------------------------------- #
# Auth entry point + precedence ladder
# --------------------------------------------------------------------------- #
def require_api_key(required: str | None = None) -> None:
    """Enforce auth per the precedence ladder documented in the module header.

    Called at the top of every blueprint handler. By default it DERIVES the
    required permission from the live ``request`` (method + blueprint). Pass an
    explicit ``required`` permission (``"read"`` / ``"write"`` / ``"admin"``) to
    OVERRIDE that derivation — e.g. the admin-invites endpoints gate BOTH their
    GET and POST on ``"admin"`` (listing/minting invites is admin-only, not the
    default ``read`` a GET would otherwise require). When auth is disabled (the
    local-only default) this is a no-op regardless of ``required``."""
    cfg = current_app.config
    issuer = cfg.get("COGNITO_ISSUER")
    if issuer:
        _require_cognito_jwt(cfg, issuer, required)
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


def _require_cognito_jwt(cfg, issuer: str, required: str | None = None) -> None:
    token = _bearer_token()
    if not token:
        abort(401, message="Missing bearer token.")
    claims = _decode_and_verify(token, cfg, issuer)
    # Stash the verified claims so handlers can identify the caller (e.g. the
    # HA-5 admin endpoints refuse to block/delete/demote the caller themselves).
    # Never populated from an unverified source — only from a passed signature.
    g.cognito_claims = claims
    required = required or _required_permission(cfg)
    granted = _effective_permissions(_token_groups(claims, cfg), _group_permissions(cfg))
    if required not in granted:
        abort(
            403,
            message=(
                f"Token's Cognito groups do not grant '{required}' "
                "(required for this request)."
            ),
        )


def _required_permission(cfg) -> str:
    """Map the current request (method + blueprint) to a required permission."""
    method = request.method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return _PERM_READ
    if request.blueprint in _ADMIN_BLUEPRINTS:
        return _PERM_ADMIN
    return _PERM_WRITE


def _group_permissions(cfg) -> dict[str, frozenset[str]]:
    """The group -> permission-set map (group names are configurable)."""
    return {
        cfg.get("AUTH_GROUP_ADMIN", "spec-admins"): frozenset(
            {_PERM_READ, _PERM_WRITE, _PERM_ADMIN}
        ),
        cfg.get("AUTH_GROUP_WRITE", "spec-writers"): frozenset(
            {_PERM_READ, _PERM_WRITE}
        ),
        cfg.get("AUTH_GROUP_READ", "spec-readers"): frozenset({_PERM_READ}),
    }


def _token_groups(claims: dict, cfg) -> list[str]:
    """Read Cognito groups from the *verified* claims only (never from a header).

    Cognito delivers ``cognito:groups`` as a JSON list on the access token; we
    also tolerate a bare string. Anything else -> no groups (no permissions)."""
    raw = claims.get(cfg.get("AUTH_GROUPS_CLAIM", "cognito:groups"))
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw]
    return []


def _effective_permissions(
    groups: list[str], mapping: dict[str, frozenset[str]]
) -> set[str]:
    """Union the permission sets of all recognised groups the token carries."""
    perms: set[str] = set()
    for g in groups:
        perms |= mapping.get(g, frozenset())
    return perms


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


def current_identity() -> dict | None:
    """Return the verified caller's ``{"sub", "username"}`` from the JWT, or None.

    Populated by ``_require_cognito_jwt`` from the *verified* access token only.
    ``None`` when Cognito auth is off (the local-only default) — callers that use
    this for a self-action guardrail therefore no-op safely in local dev, where
    the admin endpoints 501 anyway (no pool configured)."""
    claims = getattr(g, "cognito_claims", None)
    if not claims:
        return None
    return {"sub": claims.get("sub"), "username": claims.get("username")}


def caller_is_global_admin() -> bool:
    """True when the VERIFIED caller carries the global admin (spec-admins) group.

    Reads ONLY the verified access token (``g.cognito_claims``) — never a request
    body/header. False when Cognito auth is off (no verified claims) or the caller
    is not a platform super-admin. Used by the ISO-4 per-project enforcement to
    let a platform admin bypass project membership."""
    claims = getattr(g, "cognito_claims", None)
    if not claims:
        return False
    admin_group = current_app.config.get("AUTH_GROUP_ADMIN", "spec-admins")
    return admin_group in _token_groups(claims, current_app.config)


def require_project_perm(slug: str, perm: str) -> None:
    """Global capability gate + per-project authorization (ISO-4).

    ALWAYS applies the existing global group gate first — this call SUBSUMES
    ``require_api_key(required=perm)``, so the platform-wide Cognito-group check
    (spec-readers/writers/admins) still applies exactly as today (401 on a
    missing/invalid token, 403 when the token's groups do not grant ``perm``).

    THEN, only when ``PROJECT_ISOLATION_ENFORCED`` is on, additionally require the
    VERIFIED caller to be a member of ``slug`` whose role grants ``perm``
    (reader=>read, writer=>read+write, admin=>read+write+admin). A platform
    super-admin (global ``spec-admins``) bypasses membership. A denial hides
    project existence from non-members: a read (``perm="read"``) 404s — routed
    through the SAME storage ``NotFound`` path so it is byte-identical to a
    genuinely-missing project — and a write/admin denial 403s.

    Fails CLOSED: when enforcement is on and the caller's identity is
    absent/unverifiable (no verified ``sub``), access is denied (404/403 per
    ``perm``), never allowed — mirroring the self-guard in
    ``app/blueprints/admin.py``. When enforcement is OFF the membership branch is
    skipped entirely, so behaviour == ``require_api_key(required=perm)`` == today.

    The caller identity is read ONLY from the verified token, NEVER from a request
    body/header/param."""
    require_api_key(required=perm)

    if not current_app.config.get("PROJECT_ISOLATION_ENFORCED"):
        return  # dormant: identical to require_api_key(required=perm)

    # Platform super-admin bypasses per-project membership entirely.
    if caller_is_global_admin():
        return

    sub = (current_identity() or {}).get("sub")
    if not sub:
        _deny_project_access(slug, perm)  # fail closed: no verified identity

    membership = current_app.storage.get_membership(slug, sub)
    if membership is None or perm not in _ROLE_PERMISSIONS.get(membership.role, frozenset()):
        _deny_project_access(slug, perm)


def _deny_project_access(slug: str, perm: str) -> None:
    """Deny a per-project access decision (ISO-4), hiding existence from non-members.

    Reads -> 404 raised via the storage ``NotFound`` path so the response is
    byte-identical to a genuinely-missing project (existence is not leaked);
    writes/admin -> 403."""
    if perm == _PERM_READ:
        raise _StorageNotFound(f"Project '{slug}' not found.")
    abort(403, message="You are not a member of this project.")


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
