"""Application factory for the Spec Server."""
from __future__ import annotations

import click
from flask import Flask

from .config import Config
from .extensions import api, db
from .storage import make_storage
from .storage.errors import BackendUnavailable, Conflict, NotFound, VersionConflict


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    _validate_config(app)

    db.init_app(app)
    api.init_app(app)

    # Import models so SQLAlchemy is aware of them before create_all / queries.
    from . import models  # noqa: F401
    from . import idempotency  # noqa: F401  (HARDEN-3: registers IdempotencyKey)

    # Storage abstraction (SLS-2 / DEC-4): blueprints call current_app.storage.
    app.storage = make_storage(app.config)
    _register_origin_lock(app)
    _register_error_handlers(app)
    _register_cors(app)

    # Plain Flask blueprint (health probes) — not part of the OpenAPI surface.
    from .blueprints.health import bp as health_bp
    app.register_blueprint(health_bp)

    # flask-smorest blueprints (documented in OpenAPI).
    from .blueprints.admin import blp as admin_blp  # HA-2
    from .blueprints.agents import blp as agents_blp
    from .blueprints.chains import blp as chains_blp  # LOG-3
    from .blueprints.enroll import blp as enroll_blp  # ONBOARD-3 public redeem
    from .blueprints.epics import blp as epics_blp
    from .blueprints.log import blp as log_blp
    from .blueprints.members import blp as members_blp  # ISO-3
    from .blueprints.ports import blp as ports_blp
    from .blueprints.projects import blp as projects_blp
    from .blueprints.reservations import blp as reservations_blp
    from .blueprints.signup import blp as signup_blp  # HA-7
    from .blueprints.tasks import blp as tasks_blp

    api.register_blueprint(projects_blp)
    api.register_blueprint(agents_blp)
    api.register_blueprint(epics_blp)
    api.register_blueprint(members_blp)  # ISO-3 project-membership management
    api.register_blueprint(tasks_blp)
    api.register_blueprint(reservations_blp)
    api.register_blueprint(ports_blp)
    api.register_blueprint(log_blp)
    api.register_blueprint(chains_blp)
    api.register_blueprint(admin_blp)
    api.register_blueprint(signup_blp)  # HA-7 public signup queue
    api.register_blueprint(enroll_blp)  # ONBOARD-3 public agent-enrollment redeem

    _register_cli(app)
    return app


def _validate_config(app: Flask) -> None:
    """Fail-closed boot guards for config foot-guns (ISO-7).

    ``PROJECT_ISOLATION_ENFORCED`` (ISO-4) authorizes project-scoped routes off
    the VERIFIED caller ``sub``, which exists ONLY on the Cognito JWT path. If it
    is turned ON while Cognito auth is not configured (``COGNITO_ISSUER`` unset —
    i.e. local/auth-off mode), ``require_project_perm`` has no verified identity
    and fails closed on EVERY project-scoped route, bricking the whole API with
    misleading 403/404s. Refuse to boot rather than come up in that state."""
    if app.config.get("PROJECT_ISOLATION_ENFORCED") and not app.config.get("COGNITO_ISSUER"):
        raise RuntimeError(
            "PROJECT_ISOLATION_ENFORCED is ON but COGNITO_ISSUER is unset: "
            "per-project isolation enforcement requires Cognito JWT auth so the "
            "caller has a verified identity to authorize against. Without it every "
            "project-scoped route fails closed (403/404), bricking the API. Set "
            "COGNITO_ISSUER, or turn PROJECT_ISOLATION_ENFORCED off."
        )


def _register_origin_lock(app: Flask) -> None:
    """Origin-lock gate (SEC-EDGE-1): staged off/warn/enforce switch.

    The raw API Gateway ``execute-api`` hostname bypasses Cloudflare's WAF/rate
    limits. Cloudflare injects a shared-secret request header (``ORIGIN_LOCK_HEADER``)
    on every path it proxies; when enforcing we reject any request lacking it, so
    the only reachable path is through Cloudflare. Staged so we never break live
    agents before confirming Cloudflare actually injects the header:

    * ``off`` (or an empty ``ORIGIN_LOCK_SECRET``) -> no-op (safe default).
    * ``warn``    -> log a WARNING on a missing/invalid header; do NOT block.
    * ``enforce`` -> 403 (generic ``Forbidden.``) on a missing/invalid header.

    Registered in ``create_app`` before the blueprints so it runs FIRST — ahead of
    the per-handler auth check. The provided header is compared to the secret in
    constant time (``hmac.compare_digest``); neither the secret nor the provided
    value is ever logged or echoed. CORS preflight ``OPTIONS`` is answered by API
    Gateway before the Lambda, so it never reaches this hook.
    """
    import hmac

    from flask import request
    from flask_smorest import abort

    mode = (app.config.get("ORIGIN_LOCK_MODE") or "off").strip().lower()
    secret = app.config.get("ORIGIN_LOCK_SECRET") or ""
    header = app.config.get("ORIGIN_LOCK_HEADER") or "X-Origin-Lock"

    # Misconfig heads-up (do not crash): enforcing with no secret degrades to off,
    # which silently disables the gate — warn once at startup so it's visible.
    if mode == "enforce" and not secret:
        app.logger.warning(
            "origin-lock: ORIGIN_LOCK_MODE=enforce but ORIGIN_LOCK_SECRET is empty; "
            "the origin gate is DISABLED (fail-open)."
        )

    # Safe default: no gate wiring at all when off or unconfigured.
    if mode == "off" or not secret:
        return

    secret_bytes = secret.encode("utf-8")

    @app.before_request
    def _origin_lock():
        # Compare on bytes: header values are latin-1 in Werkzeug, and
        # hmac.compare_digest on str raises TypeError for non-ASCII — comparing
        # bytes keeps a hostile non-ASCII header a clean mismatch, never a 500.
        provided = (request.headers.get(header) or "").encode("utf-8", "ignore")
        if hmac.compare_digest(provided, secret_bytes):
            return None
        if mode == "warn":
            app.logger.warning(
                "origin-lock: request without valid origin header (path=%s)",
                request.path,
            )
            return None
        # enforce: generic 403 with no hint that an origin header is expected.
        abort(403, message="Forbidden.")


def _register_error_handlers(app: Flask) -> None:
    """Map backend-neutral storage errors to the HTTP status codes the API used
    before the storage abstraction. The body matches the flask-smorest error
    envelope the old ``abort(...)`` calls produced (``code``/``status``/``message``)
    so API consumers see byte-identical error responses."""
    from flask import jsonify
    from werkzeug.exceptions import RequestEntityTooLarge
    from werkzeug.http import HTTP_STATUS_CODES

    def _handler(status: int):
        def handle(err):
            return jsonify(
                code=status,
                status=HTTP_STATUS_CODES.get(status, "Unknown"),
                message=str(err),
            ), status
        return handle

    app.register_error_handler(NotFound, _handler(404))
    app.register_error_handler(Conflict, _handler(409))
    app.register_error_handler(VersionConflict, _handler(412))
    app.register_error_handler(BackendUnavailable, _handler(503))

    def _too_large(err):  # PORT-6: oversize import body -> a useful 413, not 500.
        limit = app.config.get("MAX_CONTENT_LENGTH")
        approx = f" (~{limit // 4096} tasks)" if limit else ""
        return jsonify(
            code=413,
            status=HTTP_STATUS_CODES.get(413, "Request Entity Too Large"),
            message=(
                f"Payload too large; the request body limit is {limit} bytes"
                f"{approx}. Split the SPEC.md into smaller imports, or raise "
                "MAX_CONTENT_LENGTH_BYTES on the server."
            ),
        ), 413

    app.register_error_handler(RequestEntityTooLarge, _too_large)


def _register_cors(app: Flask) -> None:
    """Config-driven CORS for the dashboard (AUTH-7).

    Echoes only exact allow-listed origins — never ``*`` — because the API is
    used with ``Authorization``/credentials, and ``*`` with credentials is both
    forbidden by browsers and unsafe. No-op when ``CORS_ORIGINS`` is empty."""
    from flask import request

    allow = set(app.config.get("CORS_ORIGINS") or [])
    if not allow:
        return

    methods = app.config.get("CORS_ALLOW_METHODS", "GET, HEAD, POST, PATCH, PUT, DELETE, OPTIONS")
    headers = app.config.get("CORS_ALLOW_HEADERS", "Authorization, Content-Type, If-Match, Idempotency-Key")
    max_age = str(app.config.get("CORS_MAX_AGE", 600))

    @app.after_request
    def _apply_cors(resp):
        origin = request.headers.get("Origin")
        if origin and origin in allow:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers.add("Vary", "Origin")
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            if request.method == "OPTIONS":
                resp.headers["Access-Control-Allow-Methods"] = methods
                resp.headers["Access-Control-Allow-Headers"] = headers
                resp.headers["Access-Control-Max-Age"] = max_age
        return resp


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db() -> None:
        """Create all tables (idempotent). Used by the container entrypoint."""
        db.create_all()
        click.echo("Spec Server schema is ready.")

    @app.cli.command("drop-db")
    def drop_db() -> None:
        """Drop all tables. Destructive — dev only."""
        db.drop_all()
        click.echo("Dropped all tables.")
