"""Environment-driven configuration for the Spec Server Flask app."""
from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Database -------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://spec:spec@localhost:5432/specserver",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Behaviour ------------------------------------------------------
    # Default lease TTL (seconds) for a claimed task.
    LEASE_DEFAULT_TTL = int(os.environ.get("LEASE_DEFAULT_TTL", "1800"))

    # Optional bearer tokens. Empty => auth disabled (local-only default).
    API_KEYS = [
        k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
    ]

    # --- flask-smorest / OpenAPI ---------------------------------------
    API_TITLE = "Spec Server"
    API_VERSION = "v1"
    OPENAPI_VERSION = "3.0.3"
    OPENAPI_URL_PREFIX = "/"
    OPENAPI_JSON_PATH = "openapi.json"
    OPENAPI_SWAGGER_UI_PATH = "/docs"
    OPENAPI_SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
    API_SPEC_OPTIONS = {
        "info": {
            "description": (
                "A concurrency-safe task/spec management API for AI coding "
                "agents. Replaces flat SPEC.md files: atomically claim the next "
                "task, complete it, and reserve collision-proof migration/table "
                "numbers. Each agent keeps its specs separate via the `owner` "
                "field on a shared per-project backlog."
            ),
        },
        "servers": [{"url": "/", "description": "This server"}],
    }


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", Config.SQLALCHEMY_DATABASE_URI
    )
    API_KEYS = []  # auth off in tests unless a test enables it
