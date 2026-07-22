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

    db.init_app(app)
    api.init_app(app)

    # Import models so SQLAlchemy is aware of them before create_all / queries.
    from . import models  # noqa: F401
    from . import idempotency  # noqa: F401  (HARDEN-3: registers IdempotencyKey)

    # Storage abstraction (SLS-2 / DEC-4): blueprints call current_app.storage.
    app.storage = make_storage(app.config)
    _register_error_handlers(app)
    _register_cors(app)

    # Plain Flask blueprint (health probes) — not part of the OpenAPI surface.
    from .blueprints.health import bp as health_bp
    app.register_blueprint(health_bp)

    # flask-smorest blueprints (documented in OpenAPI).
    from .blueprints.admin import blp as admin_blp  # HA-2
    from .blueprints.agents import blp as agents_blp
    from .blueprints.chains import blp as chains_blp  # LOG-3
    from .blueprints.epics import blp as epics_blp
    from .blueprints.log import blp as log_blp
    from .blueprints.ports import blp as ports_blp
    from .blueprints.projects import blp as projects_blp
    from .blueprints.reservations import blp as reservations_blp
    from .blueprints.signup import blp as signup_blp  # HA-7
    from .blueprints.tasks import blp as tasks_blp

    api.register_blueprint(projects_blp)
    api.register_blueprint(agents_blp)
    api.register_blueprint(epics_blp)
    api.register_blueprint(tasks_blp)
    api.register_blueprint(reservations_blp)
    api.register_blueprint(ports_blp)
    api.register_blueprint(log_blp)
    api.register_blueprint(chains_blp)
    api.register_blueprint(admin_blp)
    api.register_blueprint(signup_blp)  # HA-7 public signup queue

    _register_cli(app)
    return app


def _register_error_handlers(app: Flask) -> None:
    """Map backend-neutral storage errors to the HTTP status codes the API used
    before the storage abstraction. The body matches the flask-smorest error
    envelope the old ``abort(...)`` calls produced (``code``/``status``/``message``)
    so API consumers see byte-identical error responses."""
    from flask import jsonify
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
