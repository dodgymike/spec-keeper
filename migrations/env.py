"""Alembic environment. Pulls metadata and the DB URL from the Flask app so
migrations always match the SQLAlchemy models (the single schema source)."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import create_app
from app.extensions import db

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build the app so every model is imported and registered on db.metadata.
flask_app = create_app()
target_metadata = db.metadata


def _url() -> str:
    return flask_app.config["SQLALCHEMY_DATABASE_URI"]


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
