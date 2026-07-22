"""Backend-neutral storage errors (SLS-2.1).

Adapters raise these instead of leaking SQLAlchemy ``IntegrityError`` or calling
``flask_smorest.abort`` directly. ``create_app`` registers one HTTP error handler
per type so status codes stay identical across backends:

    NotFound            -> 404
    Conflict            -> 409
    VersionConflict     -> 412   (optimistic-lock / If-Match mismatch)
    BackendUnavailable  -> 503   (Postgres down / DynamoDB throttle after retries)
"""
from __future__ import annotations


class StorageError(Exception):
    """Base class for all backend-neutral storage errors."""


class NotFound(StorageError):
    """A requested entity (project/epic/task/... by slug/key/public_id) is absent."""


class Conflict(StorageError):
    """A uniqueness constraint was violated (duplicate key / reservation value)."""


class VersionConflict(StorageError):
    """Optimistic-lock mismatch: the caller's If-Match version is stale (-> 412)."""


class BackendUnavailable(StorageError):
    """The backend is unreachable/throttled after retries (-> 503)."""
