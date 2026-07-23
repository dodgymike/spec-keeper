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
    # Which storage adapter to use: "postgres" (reference/default) or "dynamodb".
    # Both are fully implemented and behave identically (parity enforced by
    # tests/test_parity.py). Default keeps behaviour identical for local/Postgres
    # runs; the dynamodb adapter reads DYNAMODB_TABLE / DYNAMODB_ENDPOINT_URL /
    # AWS_* from the environment.
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "postgres")

    # --- Behaviour ------------------------------------------------------
    # Default lease TTL (seconds) for a claimed task.
    LEASE_DEFAULT_TTL = int(os.environ.get("LEASE_DEFAULT_TTL", "1800"))

    # --- Request body size (PORT-6) -------------------------------------
    # Global max request body in bytes. Flask returns a clean 413 above this
    # instead of a bare 500 mid-read. The SPEC.md import is the only large body;
    # 8 MiB comfortably fits well over 2,000 tasks (a fat task line is ~200 B, so
    # 2,000 tasks with proofs+descriptions is < 1 MiB). Raise via env if needed.
    MAX_CONTENT_LENGTH = int(
        os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(8 * 1024 * 1024))
    )

    # --- Per-project isolation (ISO-4) ---------------------------------
    # When True, project-scoped routes additionally require the VERIFIED caller
    # to be a member of the project whose role grants the route's permission
    # (reader=>read, writer=>read+write, admin=>read+write+admin); a global
    # spec-admin bypasses. A non-member read 404s (existence hidden), a non-member
    # write/admin 403s. Default False => DORMANT: behaviour is byte-for-byte
    # identical to the global-group-only model. Flip ON only after every project
    # has its creator-admin + intended members backfilled (creator-auto-admin at
    # create_project runs regardless of this flag, so it is safe to flip later).
    PROJECT_ISOLATION_ENFORCED = _bool("PROJECT_ISOLATION_ENFORCED", False)

    # --- Invites (HA-2) -------------------------------------------------
    # Dedicated DynamoDB invites table backing invite-only human signup (NOT the
    # app single-table store). The admin endpoints (POST/GET /api/v1/admin/invites)
    # read/write it via boto3. Wired from terraform output `invites_table_name`.
    # UNSET (the local-dev default) => the admin invite endpoints return 501 so a
    # local run without the table is graceful rather than crashing.
    INVITES_TABLE = os.environ.get("INVITES_TABLE") or None
    # Days a freshly-minted invite stays valid (TTL). ~14d mirrors the bird gate.
    INVITE_TTL_DAYS = int(os.environ.get("INVITE_TTL_DAYS", "14"))
    # Base URL the join link is built from (e.g. https://spec.elasticninja.com).
    # The plaintext code is appended as ?code=<code>; empty => a relative link.
    INVITE_JOIN_BASE_URL = os.environ.get("INVITE_JOIN_BASE_URL", "")
    # DynamoDB endpoint override for local dev / tests (e.g. DynamoDB Local).
    DYNAMODB_ENDPOINT_URL = os.environ.get("DYNAMODB_ENDPOINT_URL") or None
    AWS_REGION = os.environ.get("AWS_REGION") or None

    # --- Agent enrollment tokens (ONBOARD-1/2) --------------------------
    # Dedicated DynamoDB table backing single-use agent-enrollment tokens (an
    # auth artifact, like invites — NOT the app single-table store). The admin
    # endpoints (POST/GET/DELETE /api/v1/admin/agent-enrollments) mint/list/revoke
    # via boto3. Wired from terraform output `agent_enrollments_table_name`. UNSET
    # (the local-dev default) => those endpoints return 501 so a local run without
    # the table is graceful rather than crashing. Only the SHA-256 token_hash is
    # ever stored; the plaintext token is returned ONCE and never persisted/logged.
    AGENT_ENROLLMENTS_TABLE = os.environ.get("AGENT_ENROLLMENTS_TABLE") or None
    # Seconds a freshly-minted enrollment token stays valid (TTL). Short-lived by
    # design (default 1h) — the redeem step (ONBOARD-3) also bounds on expires_at.
    ENROLL_TTL_SECONDS = int(os.environ.get("ENROLL_TTL_SECONDS", "3600"))
    # Base URL the enrollment link is built from (the UI origin). The plaintext
    # token rides in the fragment: f"{ENROLL_BASE_URL}/enroll#token=<token>".
    ENROLL_BASE_URL = os.environ.get("ENROLL_BASE_URL", "https://spec.elasticninja.com")
    # --- Agent enrollment REDEEM (ONBOARD-3) response knobs -------------------
    # The public redeem endpoint (POST /api/v1/agent-enrollments/redeem) hands a
    # newly-provisioned agent everything it needs to talk to the cloud API. These
    # describe the DEPLOYED API/pool so the returned recipe is copy-paste ready.
    # API base URL the onboarded agent should call (the deployed API origin).
    ENROLL_API_BASE = os.environ.get("ENROLL_API_BASE", "https://api.spec.elasticninja.com")
    # Cognito app-client id the agent mints tokens against (USER_PASSWORD_AUTH,
    # no client secret). UNSET => omitted from the recipe. Wired from terraform.
    ENROLL_COGNITO_CLIENT_ID = os.environ.get("ENROLL_COGNITO_CLIENT_ID") or None
    # Email-as-username domain for agent sign-in aliases (mirrors
    # scripts/enrol_agents.py): the alias is f"{agent_name}@{ENROLL_AGENT_DOMAIN}".
    ENROLL_AGENT_DOMAIN = os.environ.get("ENROLL_AGENT_DOMAIN", "agents.spec-server.internal")

    # --- Public request->approve signup queue (HA-7, bird Path A) ----------
    # The heavy public self-service path: POST /api/v1/signup (uniform-202
    # anti-enumeration intake) -> SQS -> worker -> GET /api/v1/validate magic
    # link -> admin approve -> provision (mint an HA-2 invite + SES join link).
    # Every knob is UNSET by default so a local run degrades gracefully: the
    # intake still returns its uniform 202 (without enqueuing), validate returns
    # the neutral "invalid", and approve provisions without sending email.
    #
    # Dedicated signups DynamoDB table (${name_prefix}-signups). Wired from the
    # signups.tf output `signups_table_name`. UNSET => validate/admin-signups
    # endpoints return the neutral/501 graceful paths.
    SIGNUPS_TABLE = os.environ.get("SIGNUPS_TABLE") or None
    # SQS intake queue URL the public POST /signup enqueues to (signups.tf output
    # `signup_intake_queue_url`). UNSET => intake returns 202 without enqueuing.
    SIGNUP_INTAKE_QUEUE_URL = os.environ.get("SIGNUP_INTAKE_QUEUE_URL") or None
    SQS_ENDPOINT_URL = os.environ.get("SQS_ENDPOINT_URL") or None
    # Per-IP fixed-window DynamoDB rate-limit counter for the public routes
    # (${name_prefix}-signup-ratelimit). UNSET => the in-app limiter fails open
    # (the edge/CDN limiter is the durable backstop).
    SIGNUP_RATELIMIT_TABLE = os.environ.get("SIGNUP_RATELIMIT_TABLE") or None
    SIGNUP_RATELIMIT_MAX = int(os.environ.get("SIGNUP_RATELIMIT_MAX", "5"))
    SIGNUP_RATELIMIT_WINDOW_S = int(os.environ.get("SIGNUP_RATELIMIT_WINDOW_S", "60"))
    # Cloudflare Turnstile server-side secret. When SET, POST /signup verifies the
    # submitted turnstile_token server-side (a failed/absent token is dropped as a
    # bot). UNSET (dev default) => the Turnstile check is skipped entirely.
    TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET") or None
    # Optional origin allow-list for the public routes. When SIGNUP_ENFORCE_ORIGIN
    # is on AND this is non-empty, POST /signup requires the Origin/Referer to
    # match one of these; UNSET/off => skipped (dev). Exact-scheme+host match.
    SIGNUP_ALLOWED_ORIGINS = [
        o.strip() for o in os.environ.get("SIGNUP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]
    SIGNUP_ENFORCE_ORIGIN = _bool("SIGNUP_ENFORCE_ORIGIN", False)
    # Optional pepper for email_hash (HMAC-SHA256). Recommended in production so a
    # leaked table cannot be dictionary-reversed; UNSET => a plain SHA-256 hash
    # (fine for local dev). MUST match between the app Lambda and the worker.
    SIGNUP_PEPPER = os.environ.get("SIGNUP_PEPPER") or None
    # Base URL the magic-link validation link is built from (e.g.
    # https://spec.elasticninja.com). The worker appends /validate?token=<link>.
    SIGNUP_VALIDATE_BASE_URL = os.environ.get("SIGNUP_VALIDATE_BASE_URL", "")
    # Per-email resend cap enforced async by the worker (never an oracle).
    SIGNUP_RESEND_CAP = int(os.environ.get("SIGNUP_RESEND_CAP", "3"))

    # --- SES transactional email (HA-6) --------------------------------------
    # Verified SES sender + configuration set for the auth/signup emails. Wired
    # from ses.tf outputs `ses_email_verification_pending`/`ses_from_address` and
    # `ses_configuration_set_name`. UNSET => email sends are skipped in dev.
    SES_FROM_ADDRESS = os.environ.get("SES_FROM_ADDRESS") or None
    SES_CONFIG_SET = os.environ.get("SES_CONFIG_SET") or None

    # Optional bearer tokens. Empty => auth disabled (local-only default).
    API_KEYS = [
        k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
    ]

    # --- Origin lock (SEC-EDGE-1) --------------------------------------------
    # The raw API Gateway execute-api hostname bypasses Cloudflare's WAF/rate
    # limits. Cloudflare injects a shared-secret request header on traffic it
    # proxies; when enforcing, the app rejects any request lacking it, so the
    # only reachable path is through Cloudflare. Staged rollout so we never break
    # live agents before confirming Cloudflare actually injects the header:
    #   off     (default) => hook is a no-op (current behaviour; safe default).
    #   warn              => log a WARNING on a missing/invalid header, do NOT block.
    #   enforce           => 403 on a missing/invalid header (fail-closed).
    # ORIGIN_LOCK_SECRET empty also degrades to off (never fail-closed with no
    # secret to compare against). The secret is compared constant-time and is
    # NEVER logged/echoed. Terraform wires these exact names.
    ORIGIN_LOCK_SECRET = os.environ.get("ORIGIN_LOCK_SECRET", "")
    ORIGIN_LOCK_MODE = os.environ.get("ORIGIN_LOCK_MODE", "off")
    ORIGIN_LOCK_HEADER = os.environ.get("ORIGIN_LOCK_HEADER", "X-Origin-Lock")

    # --- Cognito JWT auth (AUTH-2, group model per AUTH-10) -------------
    # Precedence ladder (see app/helpers.require_api_key):
    #   1. COGNITO_ISSUER set  -> require & validate a Cognito RS256 JWT and
    #                             enforce the permission derived from the token's
    #                             cognito:groups membership.
    #   2. else API_KEYS set   -> static bearer check (backward-compat).
    #   3. else                -> open (local-only default, unchanged).
    #
    # COGNITO_ISSUER is the OIDC issuer (terraform output `cognito_issuer_url`).
    # COGNITO_JWKS_URI defaults to "<issuer>/.well-known/jwks.json" when unset
    # (terraform output `cognito_jwks_uri`).
    COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER") or None
    COGNITO_JWKS_URI = os.environ.get("COGNITO_JWKS_URI") or None
    # Accepted audiences (aud) OR client_id values — now the *agents* app-client
    # id plus the *UI* app-client id (comma-separated). Cognito access tokens
    # carry no `aud`, so we match `client_id` too. Empty => audience check
    # skipped. e.g. COGNITO_AUDIENCE=<agents_client_id>,<ui_client_id>
    COGNITO_AUDIENCE = [
        a.strip() for a in os.environ.get("COGNITO_AUDIENCE", "").split(",") if a.strip()
    ]
    # Expected `token_use` claim. Cognito access tokens use "access".
    COGNITO_TOKEN_USE = os.environ.get("COGNITO_TOKEN_USE", "access") or None
    # JWKS cache TTL (seconds) and clock leeway (seconds) for exp/nbf.
    JWKS_CACHE_TTL = int(os.environ.get("JWKS_CACHE_TTL", "3600"))
    # Min seconds between JWKS refetches driven by an unknown `kid` (bounds a
    # bogus-kid flood into at most one outbound fetch per interval — anti-DoS).
    JWKS_MIN_REFRESH_INTERVAL = int(os.environ.get("JWKS_MIN_REFRESH_INTERVAL", "30"))
    AUTH_LEEWAY = int(os.environ.get("AUTH_LEEWAY", "0"))
    # Access-token claim carrying the caller's Cognito group list.
    AUTH_GROUPS_CLAIM = os.environ.get("AUTH_GROUPS_CLAIM", "cognito:groups")
    # Group names -> permissions (union'd per user in app/helpers):
    #   AUTH_GROUP_ADMIN  => {read, write, admin}   (project/agent management)
    #   AUTH_GROUP_WRITE  => {read, write}          (task/epic/reservation/... writes)
    #   AUTH_GROUP_READ   => {read}                 (GET/HEAD)
    AUTH_GROUP_ADMIN = os.environ.get("AUTH_GROUP_ADMIN", "spec-admins")
    AUTH_GROUP_WRITE = os.environ.get("AUTH_GROUP_WRITE", "spec-writers")
    AUTH_GROUP_READ = os.environ.get("AUTH_GROUP_READ", "spec-readers")

    # --- Admin user-lifecycle API (HA-5) --------------------------------
    # Cognito user pool the admin endpoints (GET/POST /api/v1/admin/users...)
    # manage humans (and agents) in, via boto3 cognito-idp admin actions.
    # Approve/reject/block/delete/promote all operate on this pool. UNSET (the
    # local-dev default) => those endpoints return 501 so a local run without a
    # pool is graceful. Wired from terraform output `cognito_user_pool_id`.
    COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID") or None

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
    # Per-project isolation OFF by default so a stray PROJECT_ISOLATION_ENFORCED
    # env var can't flip the baseline; the ISO-4 tests set it on a subclass.
    PROJECT_ISOLATION_ENFORCED = False
    COGNITO_ISSUER = None
    COGNITO_JWKS_URI = None
    COGNITO_AUDIENCE = []
    CORS_ORIGINS = []
    # Invites off by default so a stray INVITES_TABLE env can't flip the baseline;
    # tests that exercise the admin endpoint set it explicitly on a subclass.
    INVITES_TABLE = None
    # Agent-enrollment token table off by default (same rationale) — tests that
    # exercise the mint/list/revoke endpoints set it explicitly on a subclass.
    AGENT_ENROLLMENTS_TABLE = None
    # User-admin pool off by default (same rationale) — tests that exercise the
    # admin user endpoints set COGNITO_USER_POOL_ID explicitly on a subclass.
    COGNITO_USER_POOL_ID = None
    # Signup queue (HA-7) off by default so a stray env var can't flip the
    # baseline; the signup tests set these explicitly on a subclass.
    SIGNUPS_TABLE = None
    SIGNUP_INTAKE_QUEUE_URL = None
    SIGNUP_RATELIMIT_TABLE = None
    TURNSTILE_SECRET = None
    SIGNUP_ALLOWED_ORIGINS = []
    SIGNUP_ENFORCE_ORIGIN = False
    SIGNUP_PEPPER = None
    SES_FROM_ADDRESS = None
    SES_CONFIG_SET = None
    # Origin lock (SEC-EDGE-1) OFF by default so a stray ORIGIN_LOCK_* env var
    # can't flip the baseline; the origin-lock tests set these on a subclass.
    ORIGIN_LOCK_SECRET = ""
    ORIGIN_LOCK_MODE = "off"
    ORIGIN_LOCK_HEADER = "X-Origin-Lock"
