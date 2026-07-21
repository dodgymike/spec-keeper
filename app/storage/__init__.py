"""Storage abstraction package (SLS-2 / DEC-4).

``make_storage`` selects a ``StorageBackend`` adapter from ``STORAGE_BACKEND``
(default ``"postgres"``), so the whole app is backend-agnostic behind
``current_app.storage``. Postgres remains the reference/default; a DynamoDB
adapter drops in later (SLS-3+) with no blueprint changes.
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
    # SLS-3: app/storage/dynamo.py -> DynamoBackend (config-selected second adapter)
    raise ValueError(f"unknown STORAGE_BACKEND {backend!r}")
