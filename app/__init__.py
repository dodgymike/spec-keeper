"""Application factory for the Spec Server."""
from __future__ import annotations

import click
from flask import Flask

from .config import Config
from .extensions import api, db


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)
    api.init_app(app)

    # Import models so SQLAlchemy is aware of them before create_all / queries.
    from . import models  # noqa: F401

    # Plain Flask blueprint (health probes) — not part of the OpenAPI surface.
    from .blueprints.health import bp as health_bp
    app.register_blueprint(health_bp)

    # flask-smorest blueprints (documented in OpenAPI).
    from .blueprints.agents import blp as agents_blp
    from .blueprints.epics import blp as epics_blp
    from .blueprints.projects import blp as projects_blp
    from .blueprints.reservations import blp as reservations_blp
    from .blueprints.tasks import blp as tasks_blp

    api.register_blueprint(projects_blp)
    api.register_blueprint(agents_blp)
    api.register_blueprint(epics_blp)
    api.register_blueprint(tasks_blp)
    api.register_blueprint(reservations_blp)

    _register_cli(app)
    return app


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
