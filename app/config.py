"""Environment-driven configuration for the Spec Server Flask app."""
from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Database -------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://spec:spec@localhost:5432/specserver",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Storage backend (DEC-4) ---------------------------------------
    # Which storage adapter to use: "postgres" (reference/default) or, later,
    # "dynamodb". Default keeps behaviour identical for local/Postgres runs.
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "postgres")

    # --- Behaviour ------------------------------------------------------
    # Default lease TTL (seconds) for a claimed task.
    LEASE_DEFAULT_TTL = int(os.environ.get("LEASE_DEFAULT_TTL", "1800"))

    # Optional bearer tokens. Empty => auth disabled (local-only default).
    API_KEYS = [
        k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
    ]

    # --- Cognito JWT auth (AUTH-2) --------------------------------------
    # Precedence ladder (see app/helpers.require_api_key):
    #   1. COGNITO_ISSUER set  -> require & validate a Cognito RS256 JWT + scope.
    #   2. else API_KEYS set   -> static bearer check (backward-compat).
    #   3. else                -> open (local-only default, unchanged).
    #
    # COGNITO_ISSUER is the OIDC issuer (terraform output `cognito_issuer_url`).
    # COGNITO_JWKS_URI defaults to "<issuer>/.well-known/jwks.json" when unset
    # (terraform output `cognito_jwks_uri`).
    COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER") or None
    COGNITO_JWKS_URI = os.environ.get("COGNITO_JWKS_URI") or None
    # Accepted audiences (aud) OR client_id values. Cognito access tokens carry
    # no `aud`, so we also accept `client_id`. Empty => audience check skipped.
    COGNITO_AUDIENCE = [
        a.strip() for a in os.environ.get("COGNITO_AUDIENCE", "").split(",") if a.strip()
    ]
    # Expected `token_use` claim. Cognito M2M access tokens use "access".
    COGNITO_TOKEN_USE = os.environ.get("COGNITO_TOKEN_USE", "access") or None
    # JWKS cache TTL (seconds) and clock leeway (seconds) for exp/nbf.
    JWKS_CACHE_TTL = int(os.environ.get("JWKS_CACHE_TTL", "3600"))
    # Min seconds between JWKS refetches driven by an unknown `kid` (bounds a
    # bogus-kid flood into at most one outbound fetch per interval — anti-DoS).
    JWKS_MIN_REFRESH_INTERVAL = int(os.environ.get("JWKS_MIN_REFRESH_INTERVAL", "30"))
    AUTH_LEEWAY = int(os.environ.get("AUTH_LEEWAY", "0"))
    # HTTP method/resource -> required scope. Custom scope *names* (the suffix of
    # the full "<resource-server>/<name>" identifier in the JWT `scope` claim).
    AUTH_SCOPE_READ = os.environ.get("AUTH_SCOPE_READ", "tasks.read")
    AUTH_SCOPE_WRITE = os.environ.get("AUTH_SCOPE_WRITE", "tasks.write")
    AUTH_SCOPE_ADMIN = os.environ.get("AUTH_SCOPE_ADMIN", "projects.admin")

    # --- CORS (AUTH-7) --------------------------------------------------
    # Exact-match allow-list of browser origins for the dashboard. Empty =>
    # CORS disabled (local-only default). "*" is intentionally NOT honoured:
    # we never reflect a wildcard while Authorization/credentials are in play.
    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
    ]
    CORS_MAX_AGE = int(os.environ.get("CORS_MAX_AGE", "600"))
    CORS_ALLOW_HEADERS = os.environ.get(
        "CORS_ALLOW_HEADERS", "Authorization, Content-Type, If-Match, Idempotency-Key"
    )
    CORS_ALLOW_METHODS = os.environ.get(
        "CORS_ALLOW_METHODS", "GET, HEAD, POST, PATCH, PUT, DELETE, OPTIONS"
    )

    # --- flask-smorest / OpenAPI ---------------------------------------
    API_TITLE = "Spec Server"
    API_VERSION = "v1"
    OPENAPI_VERSION = "3.0.3"
    OPENAPI_URL_PREFIX = "/"
    OPENAPI_JSON_PATH = "openapi.json"
    OPENAPI_SWAGGER_UI_PATH = "/docs"
    OPENAPI_SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
    API_SPEC_OPTIONS = {
        "info": {
            "description": (
                "A concurrency-safe task/spec management API for AI coding "
                "agents. Replaces flat SPEC.md files: atomically claim the next "
                "task, complete it, and reserve collision-proof migration/table "
                "numbers. Each agent keeps its specs separate via the `owner` "
                "field on a shared per-project backlog."
            ),
        },
        "servers": [{"url": "/", "description": "This server"}],
    }


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", Config.SQLALCHEMY_DATABASE_URI
    )
    # Auth OFF in tests unless a test opts in via its own config subclass —
    # pinned here so a stray COGNITO_*/CORS_* env var can't flip the baseline.
    API_KEYS = []
    COGNITO_ISSUER = None
    COGNITO_JWKS_URI = None
    COGNITO_AUDIENCE = []
    CORS_ORIGINS = []
