"""Pytest fixtures. Tests require a real PostgreSQL (the atomic features —
FOR UPDATE SKIP LOCKED, ON CONFLICT upsert, partial unique indexes — are
Postgres-specific). Point TEST_DATABASE_URL at a throwaway database.
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from app import create_app
from app.config import TestConfig
from app.extensions import db

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://spec:spec@localhost:5432/specserver",
)


@pytest.fixture(scope="session")
def app():
    class _Cfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = TEST_DB_URL

    app = create_app(_Cfg)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.drop_all()


@pytest.fixture(autouse=True)
def _clean_tables(app):
    """Truncate all tables between tests for isolation."""
    yield
    with app.app_context():
        meta = db.metadata
        tables = ",".join(f'"{t.name}"' for t in reversed(meta.sorted_tables))
        db.session.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def project(client):
    """Create and return a fresh project slug."""
    resp = client.post("/api/v1/projects", json={"slug": "demo", "name": "Demo"})
    assert resp.status_code == 201, resp.get_json()
    return "demo"
