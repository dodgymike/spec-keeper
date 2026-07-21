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

    # Plain Flask blueprint (health probes) — not part of the OpenAPI surface.
    from .blueprints.health import bp as health_bp
    app.register_blueprint(health_bp)

    # flask-smorest blueprints (documented in OpenAPI).
    from .blueprints.agents import blp as agents_blp
    from .blueprints.chains import blp as chains_blp  # LOG-3
    from .blueprints.epics import blp as epics_blp
    from .blueprints.log import blp as log_blp
    from .blueprints.ports import blp as ports_blp
    from .blueprints.projects import blp as projects_blp
    from .blueprints.reservations import blp as reservations_blp
    from .blueprints.tasks import blp as tasks_blp

    api.register_blueprint(projects_blp)
    api.register_blueprint(agents_blp)
    api.register_blueprint(epics_blp)
    api.register_blueprint(tasks_blp)
    api.register_blueprint(reservations_blp)
    api.register_blueprint(ports_blp)
    api.register_blueprint(log_blp)
    api.register_blueprint(chains_blp)

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
