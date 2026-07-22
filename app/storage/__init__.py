"""Storage abstraction package (SLS-2 / DEC-4).

``make_storage`` selects a ``StorageBackend`` adapter from ``STORAGE_BACKEND``
(default ``"postgres"``), so the whole app is backend-agnostic behind
``current_app.storage``. Postgres remains the reference/default; ``"dynamodb"``
selects the ``DynamoBackend`` adapter (SLS-3..SLS-8) with no blueprint changes —
the public HTTP API is identical on both backends.
"""
from __future__ import annotations

from .base import StorageBackend
from .errors import (
    BackendUnavailable,
    Conflict,
    NotFound,
    StorageError,
    VersionConflict,
)

__all__ = [
    "StorageBackend",
    "make_storage",
    "StorageError",
    "NotFound",
    "Conflict",
    "VersionConflict",
    "BackendUnavailable",
]


def make_storage(config) -> StorageBackend:
    """Build the storage backend chosen by ``config['STORAGE_BACKEND']``."""
    backend = config.get("STORAGE_BACKEND", "postgres")
    if backend == "postgres":
        from .postgres import PostgresBackend
        return PostgresBackend()
    if backend == "dynamodb":
        # SLS-3: DynamoDB adapter. Its connection settings (table, region,
        # DYNAMODB_ENDPOINT_URL, credentials) are read from os.environ inside
        # the storage layer (app/config.py is intentionally untouched — a
        # parallel agent owns it), so nothing here needs plumbing from config.
        from .dynamo import DynamoBackend
        return DynamoBackend()
    raise ValueError(f"unknown STORAGE_BACKEND {backend!r}")
