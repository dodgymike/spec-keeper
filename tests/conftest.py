"""Pytest fixtures.

Backends (SLS-8 parity): the ``app`` fixture is parametrised over
``STORAGE_BACKEND`` so the SAME behavioural + concurrency tests run against both
the reference Postgres adapter and the DynamoDB adapter.

* **Postgres** requires a real PostgreSQL (the atomic features — FOR UPDATE SKIP
  LOCKED, ON CONFLICT upsert, partial unique indexes — are Postgres-specific).
  Point ``TEST_DATABASE_URL`` at a throwaway database.
* **DynamoDB** requires DynamoDB Local (the guarantees are conditional-write
  specific, just as the Postgres ones are skip-locked specific — DEC-3 applies to
  both). Point ``DYNAMODB_ENDPOINT_URL`` at it (see docker-compose.dynamodb.yml).

Which backends run is controlled by ``TEST_BACKENDS`` (comma-separated, default
``postgres``). A ``dynamodb`` param self-skips when no ``DYNAMODB_ENDPOINT_URL``
is configured, so the plain Postgres command stays green with zero setup.

Tests that reach into SQLAlchemy/ORM internals (``db.session``, ``app.services``)
are Postgres-implementation-specific; mark them ``@pytest.mark.postgres_only`` and
they are skipped on the DynamoDB param. Behavioural equivalents that go through
the HTTP API run cross-backend (see ``test_parity.py``).
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from app import create_app
from app.config import TestConfig
from app.extensions import db

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://spec:spec@localhost:5432/specserver",
)

_BACKENDS = [b.strip() for b in os.environ.get("TEST_BACKENDS", "postgres").split(",")
             if b.strip()]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres_only: test asserts on SQLAlchemy/ORM internals; skip on DynamoDB.",
    )


# --------------------------------------------------------------------------- #
# DynamoDB Local table lifecycle (mirrors infra/terraform/dynamodb.tf)
# --------------------------------------------------------------------------- #
_GSI_PROJECTIONS = {
    "GSI1": "ALL", "GSI2": "ALL", "GSI3": "KEYS_ONLY", "GSI4": "ALL", "GSI5": "ALL",
    # GSI6 (ISO-1): list a principal's projects; member items carry the full DTO
    # so list_projects_for_principal returns without a follow-up read -> ALL.
    "GSI6": "ALL",
}


def _create_dynamo_table(client, table_name):
    attrs = [("PK", "S"), ("SK", "S")]
    gsis = []
    for i in range(1, 7):
        attrs += [(f"GSI{i}PK", "S"), (f"GSI{i}SK", "S")]
        gsis.append({
            "IndexName": f"GSI{i}",
            "KeySchema": [
                {"AttributeName": f"GSI{i}PK", "KeyType": "HASH"},
                {"AttributeName": f"GSI{i}SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": _GSI_PROJECTIONS[f"GSI{i}"]},
        })
    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": t} for n, t in attrs
        ],
        GlobalSecondaryIndexes=gsis,
    )
    client.get_waiter("table_exists").wait(TableName=table_name)


def _dynamo_clients(endpoint, region):
    import boto3
    session = boto3.session.Session()
    client = session.client("dynamodb", endpoint_url=endpoint, region_name=region)
    resource = session.resource("dynamodb", endpoint_url=endpoint, region_name=region)
    return client, resource


# --------------------------------------------------------------------------- #
# App fixture, parametrised over storage backend
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", params=_BACKENDS)
def app(request):
    backend = request.param

    if backend == "postgres":
        class _Cfg(TestConfig):
            STORAGE_BACKEND = "postgres"
            SQLALCHEMY_DATABASE_URI = TEST_DB_URL

        application = create_app(_Cfg)
        with application.app_context():
            db.drop_all()
            db.create_all()
        application._backend = "postgres"
        yield application
        with application.app_context():
            db.drop_all()
        return

    if backend == "dynamodb":
        endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
        if not endpoint:
            pytest.skip("DynamoDB Local not configured (set DYNAMODB_ENDPOINT_URL).")
        region = os.environ.get("AWS_REGION", "us-east-1")
        table_name = f"spec-test-{uuid.uuid4().hex[:8]}"
        os.environ["DYNAMODB_TABLE"] = table_name
        os.environ["DYNAMODB_ENDPOINT_URL"] = endpoint

        client, resource = _dynamo_clients(endpoint, region)
        _create_dynamo_table(client, table_name)

        class _Cfg(TestConfig):
            STORAGE_BACKEND = "dynamodb"
            SQLALCHEMY_DATABASE_URI = TEST_DB_URL  # unused by the dynamo adapter

        application = create_app(_Cfg)
        application._backend = "dynamodb"
        application._dynamo_table = resource.Table(table_name)
        yield application
        client.delete_table(TableName=table_name)
        return

    raise ValueError(f"unknown backend {backend!r}")


@pytest.fixture(autouse=True)
def _backend_guard(request):
    """Skip Postgres-implementation-specific tests on the DynamoDB param.

    Depends on ``app`` lazily (only when the test actually uses it) so tests that
    build their own app instances — e.g. ``test_auth.py`` — are NOT dragged into
    the backend parametrisation."""
    if "app" not in request.fixturenames:
        return
    app = request.getfixturevalue("app")
    if app._backend == "dynamodb" and request.node.get_closest_marker("postgres_only"):
        pytest.skip("postgres-only test (asserts on SQLAlchemy/ORM internals).")


@pytest.fixture(autouse=True)
def _clean_tables(request):
    """Isolate tests: wipe all data between them, per backend.

    Lazily resolves ``app`` so it is a no-op for tests that don't use the shared
    app fixture (keeping ``test_auth.py``'s self-built apps independent)."""
    yield
    if "app" not in request.fixturenames:
        return
    app = request.getfixturevalue("app")
    if app._backend == "postgres":
        with app.app_context():
            meta = db.metadata
            tables = ",".join(f'"{t.name}"' for t in reversed(meta.sorted_tables))
            db.session.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            db.session.commit()
    else:
        table = app._dynamo_table
        scan = table.scan(ProjectionExpression="PK,SK")
        items = scan.get("Items", [])
        while scan.get("LastEvaluatedKey"):
            scan = table.scan(ProjectionExpression="PK,SK",
                              ExclusiveStartKey=scan["LastEvaluatedKey"])
            items += scan.get("Items", [])
        with table.batch_writer() as bw:
            for it in items:
                bw.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def project(client):
    """Create and return a fresh project slug."""
    resp = client.post("/api/v1/projects", json={"slug": "demo", "name": "Demo"})
    assert resp.status_code == 201, resp.get_json()
    return "demo"
