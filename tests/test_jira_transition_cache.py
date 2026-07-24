"""Tests for Jira transition cache warmup (JIRA-6).

Covers:
- warm_transition_cache fetches statuses and stores in cached_transitions
- find_transition returns a match from cache (case-insensitive)
- find_transition refreshes cache once on miss, then finds the transition
- find_transition raises TransitionNotFoundError after refresh if still missing
- POST /jira-config with enabled=True triggers cache warmup
- PUT /jira-config enabling triggers cache warmup
- Cache warmup failure does not block config save (best-effort)

All Jira HTTP calls are mocked (no live Jira needed), following the pattern
established in tests/test_jira_client.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.jira_client import JiraClient, JiraClientError
from app.jira_transitions import (
    TransitionCacheError,
    TransitionNotFoundError,
    _find_in_cache,
    find_transition,
    warm_transition_cache,
)
from app.models import JiraProjectConfig, Project


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Ensure the Fernet key env var is set for all tests in this module."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


@pytest.fixture
def jira_project(client):
    """Create a fresh project for jira transition cache tests."""
    resp = client.post(
        "/api/v1/projects", json={"slug": "tcache-proj", "name": "TCache Proj"}
    )
    assert resp.status_code == 201, resp.get_json()
    return "tcache-proj"


@pytest.fixture
def jira_config(app, jira_project, client):
    """Create a JiraProjectConfig row in the DB (with warmup mocked out)."""
    from app.crypto import encrypt

    with app.app_context():
        project = db.session.execute(
            db.select(Project).where(Project.slug == jira_project)
        ).scalar_one()
        config = JiraProjectConfig(
            project_id=project.id,
            base_url="https://test.atlassian.net",
            email="agent@example.com",
            api_token_encrypted=encrypt("secret-token"),
            jira_project_key="PROJ",
            enabled=True,
        )
        db.session.add(config)
        db.session.commit()
        db.session.refresh(config)
        return config


# --- Statuses response fixture (mimics Jira project statuses API) ---

MOCK_PROJECT_STATUSES_RESPONSE = [
    {
        "id": "10001",
        "name": "Task",
        "statuses": [
            {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
            {"id": "3", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            {"id": "5", "name": "Done", "statusCategory": {"key": "done"}},
        ],
    },
    {
        "id": "10002",
        "name": "Bug",
        "statuses": [
            {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
            {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}},
            {"id": "5", "name": "Done", "statusCategory": {"key": "done"}},
        ],
    },
]


def _mock_statuses_response():
    """Create a mock response for the project statuses endpoint."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = MOCK_PROJECT_STATUSES_RESPONSE
    return mock_resp


class TestWarmTransitionCache:
    """Tests for warm_transition_cache function."""

    def test_transition_cache_fetches_and_stores(self, app, jira_config):
        """warm_transition_cache fetches statuses and stores them in the DB."""
        with app.app_context():
            config = db.session.merge(jira_config)
            with patch(
                "app.jira_transitions.JiraClient._request",
                return_value=_mock_statuses_response(),
            ):
                result = warm_transition_cache(config)

            # Should return deduplicated statuses
            assert len(result) == 4  # Open, In Progress, Done, In Review
            names = {s["name"] for s in result}
            assert "Done" in names
            assert "Open" in names
            assert "In Progress" in names
            assert "In Review" in names

            # Check DB persistence
            db.session.refresh(config)
            assert config.cached_transitions is not None
            assert "statuses" in config.cached_transitions
            assert "fetched_at" in config.cached_transitions
            assert len(config.cached_transitions["statuses"]) == 4

    def test_transition_cache_deduplicates_statuses(self, app, jira_config):
        """Statuses appearing in multiple issue types are deduplicated by id."""
        with app.app_context():
            config = db.session.merge(jira_config)
            with patch(
                "app.jira_transitions.JiraClient._request",
                return_value=_mock_statuses_response(),
            ):
                result = warm_transition_cache(config)

            # "Open" (id=1) and "Done" (id=5) appear in both Task and Bug
            ids = [s["id"] for s in result]
            assert ids.count("1") == 1  # Open only once
            assert ids.count("5") == 1  # Done only once

    def test_transition_cache_error_on_no_token(self, app, jira_config):
        """warm_transition_cache raises TransitionCacheError if no token set."""
        with app.app_context():
            config = db.session.merge(jira_config)
            config.api_token_encrypted = None
            db.session.commit()

            with pytest.raises(TransitionCacheError, match="no API token"):
                warm_transition_cache(config)

    def test_transition_cache_error_on_jira_failure(self, app, jira_config):
        """warm_transition_cache raises TransitionCacheError on Jira API error."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with app.app_context():
            config = db.session.merge(jira_config)
            with patch(
                "app.jira_transitions.JiraClient._request",
                return_value=mock_resp,
            ):
                # JiraClient._request raises on non-ok, so we patch differently
                pass

        # Need to patch at the _request level properly
        with app.app_context():
            config = db.session.merge(jira_config)
            with patch(
                "app.jira_transitions._fetch_project_statuses",
                side_effect=JiraClientError(403, "Forbidden", "GET", "http://x"),
            ):
                with pytest.raises(TransitionCacheError, match="HTTP 403"):
                    warm_transition_cache(config)


class TestFindTransition:
    """Tests for find_transition function (refresh-once-before-failing)."""

    def test_transition_cache_find_in_existing_cache(self, app, jira_config):
        """find_transition returns a match from existing cache without refresh."""
        with app.app_context():
            config = db.session.merge(jira_config)
            # Pre-populate cache
            config.cached_transitions = {
                "statuses": [
                    {"id": "1", "name": "Open"},
                    {"id": "5", "name": "Done"},
                ],
                "fetched_at": "2026-07-01T00:00:00+00:00",
            }
            db.session.commit()

            result = find_transition(config, "Done")
            assert result == {"id": "5", "name": "Done"}

    def test_transition_cache_find_case_insensitive(self, app, jira_config):
        """find_transition matches case-insensitively."""
        with app.app_context():
            config = db.session.merge(jira_config)
            config.cached_transitions = {
                "statuses": [{"id": "5", "name": "Done"}],
                "fetched_at": "2026-07-01T00:00:00+00:00",
            }
            db.session.commit()

            result = find_transition(config, "done")
            assert result == {"id": "5", "name": "Done"}

            result2 = find_transition(config, "DONE")
            assert result2 == {"id": "5", "name": "Done"}

    def test_transition_cache_refresh_on_miss(self, app, jira_config):
        """find_transition refreshes cache once when transition not found."""
        with app.app_context():
            config = db.session.merge(jira_config)
            # Start with empty cache
            config.cached_transitions = None
            db.session.commit()

            with patch(
                "app.jira_transitions._fetch_project_statuses",
                return_value=[
                    {"id": "1", "name": "Open"},
                    {"id": "5", "name": "Done"},
                ],
            ):
                result = find_transition(config, "Done")

            assert result == {"id": "5", "name": "Done"}
            # Cache should now be populated
            assert config.cached_transitions is not None
            assert len(config.cached_transitions["statuses"]) == 2

    def test_transition_cache_not_found_after_refresh(self, app, jira_config):
        """find_transition raises TransitionNotFoundError if still missing after refresh."""
        with app.app_context():
            config = db.session.merge(jira_config)
            config.cached_transitions = None
            db.session.commit()

            with patch(
                "app.jira_transitions._fetch_project_statuses",
                return_value=[
                    {"id": "1", "name": "Open"},
                    {"id": "3", "name": "In Progress"},
                ],
            ):
                with pytest.raises(
                    TransitionNotFoundError,
                    match="not found.*even after refresh",
                ):
                    find_transition(config, "Done")

    def test_transition_cache_no_refresh_when_disabled(self, app, jira_config):
        """find_transition raises immediately if allow_refresh=False."""
        with app.app_context():
            config = db.session.merge(jira_config)
            config.cached_transitions = {
                "statuses": [{"id": "1", "name": "Open"}],
                "fetched_at": "2026-07-01T00:00:00+00:00",
            }
            db.session.commit()

            with pytest.raises(TransitionNotFoundError, match="refresh disabled"):
                find_transition(config, "Done", allow_refresh=False)

    def test_transition_cache_refreshes_exactly_once(self, app, jira_config):
        """find_transition calls _fetch_project_statuses at most once."""
        with app.app_context():
            config = db.session.merge(jira_config)
            config.cached_transitions = None
            db.session.commit()

            with patch(
                "app.jira_transitions._fetch_project_statuses",
                return_value=[{"id": "1", "name": "Open"}],
            ) as mock_fetch:
                with pytest.raises(TransitionNotFoundError):
                    find_transition(config, "NonExistent")

            # Only called once (the refresh), not retried
            mock_fetch.assert_called_once()


class TestEndpointTriggersWarmup:
    """Tests that POST/PUT /jira-config triggers transition cache warmup."""

    def test_transition_cache_post_triggers_warmup(self, client, jira_project, app):
        """POST with enabled=True triggers cache warmup."""
        with patch(
            "app.blueprints.jira_config.warm_transition_cache"
        ) as mock_warm:
            resp = client.post(
                f"/api/v1/projects/{jira_project}/jira-config",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "agent@example.com",
                    "api_token": "secret-token",
                    "jira_project_key": "PROJ",
                    "enabled": True,
                },
            )
            assert resp.status_code == 201, resp.get_json()
            mock_warm.assert_called_once()

    def test_transition_cache_post_no_warmup_when_disabled(
        self, client, jira_project
    ):
        """POST with enabled=False does NOT trigger cache warmup."""
        with patch(
            "app.blueprints.jira_config.warm_transition_cache"
        ) as mock_warm:
            resp = client.post(
                f"/api/v1/projects/{jira_project}/jira-config",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "agent@example.com",
                    "api_token": "secret-token",
                    "jira_project_key": "PROJ",
                    "enabled": False,
                },
            )
            assert resp.status_code == 201
            mock_warm.assert_not_called()

    def test_transition_cache_put_triggers_warmup(self, client, jira_project):
        """PUT enabling config triggers cache warmup."""
        # Create disabled config first
        with patch(
            "app.blueprints.jira_config.warm_transition_cache"
        ):
            client.post(
                f"/api/v1/projects/{jira_project}/jira-config",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "agent@example.com",
                    "api_token": "secret-token",
                    "jira_project_key": "PROJ",
                    "enabled": False,
                },
            )

        # Now enable via PUT
        with patch(
            "app.blueprints.jira_config.warm_transition_cache"
        ) as mock_warm:
            resp = client.put(
                f"/api/v1/projects/{jira_project}/jira-config",
                json={"enabled": True},
            )
            assert resp.status_code == 200
            mock_warm.assert_called_once()

    def test_transition_cache_warmup_failure_does_not_block_save(
        self, client, jira_project, app
    ):
        """If cache warmup fails, config is still saved successfully."""
        with patch(
            "app.blueprints.jira_config.warm_transition_cache",
            side_effect=TransitionCacheError("Jira unreachable"),
        ):
            resp = client.post(
                f"/api/v1/projects/{jira_project}/jira-config",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "agent@example.com",
                    "api_token": "secret-token",
                    "jira_project_key": "PROJ",
                    "enabled": True,
                },
            )
            # Config save still succeeds
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["enabled"] is True


class TestFindInCache:
    """Tests for the internal _find_in_cache helper."""

    def test_transition_cache_find_none_when_empty(self):
        """_find_in_cache returns None when cached_transitions is None."""
        config = MagicMock()
        config.cached_transitions = None
        assert _find_in_cache(config, "Done") is None

    def test_transition_cache_find_none_when_no_match(self):
        """_find_in_cache returns None when name not present."""
        config = MagicMock()
        config.cached_transitions = {
            "statuses": [{"id": "1", "name": "Open"}]
        }
        assert _find_in_cache(config, "Done") is None

    def test_transition_cache_find_match(self):
        """_find_in_cache returns the matching status dict."""
        config = MagicMock()
        config.cached_transitions = {
            "statuses": [
                {"id": "1", "name": "Open"},
                {"id": "5", "name": "Done"},
            ]
        }
        result = _find_in_cache(config, "Done")
        assert result == {"id": "5", "name": "Done"}
