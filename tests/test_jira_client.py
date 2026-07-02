"""Unit tests for app.jira_client — the thin Jira Cloud REST API wrapper.

These tests use unittest.mock to patch requests.Session methods; no live
Jira instance is needed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.jira_client import JiraClient, JiraClientError


@pytest.fixture
def client():
    """A JiraClient instance with test credentials."""
    return JiraClient(
        base_url="https://test.atlassian.net",
        email="agent@example.com",
        api_token="secret-token-value",
    )


class TestCreateIssue:
    """Tests for JiraClient.create_issue."""

    def test_success_returns_key(self, client):
        """create_issue sends correct request and returns the issue key."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "10001",
            "key": "PROJ-42",
            "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        }

        with patch.object(client._session, "request", return_value=mock_resp) as mock_req:
            result = client.create_issue("PROJ", "Fix the bug", "Detailed desc")

        assert result == "PROJ-42"
        # Verify the HTTP call
        mock_req.assert_called_once()
        args, kwargs = mock_req.call_args
        assert args[0] == "POST"
        assert args[1] == "https://test.atlassian.net/rest/api/3/issue"
        payload = kwargs["json"]
        assert payload["fields"]["project"]["key"] == "PROJ"
        assert payload["fields"]["summary"] == "Fix the bug"
        assert payload["fields"]["issuetype"]["name"] == "Task"
        # description is ADF
        desc_content = payload["fields"]["description"]["content"][0]["content"][0]
        assert desc_content["text"] == "Detailed desc"

    def test_custom_issue_type(self, client):
        """create_issue honours a custom issue_type parameter."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"key": "PROJ-99"}

        with patch.object(client._session, "request", return_value=mock_resp):
            result = client.create_issue("PROJ", "Epic task", "Desc", issue_type="Bug")

        assert result == "PROJ-99"

    def test_auth_header_uses_basic(self, client):
        """The session is configured with HTTP Basic auth (email + token)."""
        assert client._session.auth == ("agent@example.com", "secret-token-value")


class TestGetTransitions:
    """Tests for JiraClient.get_transitions."""

    def test_parses_transitions_list(self, client):
        """get_transitions returns a cleaned list of id/name dicts."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "transitions": [
                {"id": "11", "name": "To Do", "extra_field": "ignored"},
                {"id": "21", "name": "In Progress", "extra_field": "ignored"},
                {"id": "31", "name": "Done", "extra_field": "ignored"},
            ]
        }

        with patch.object(client._session, "request", return_value=mock_resp) as mock_req:
            result = client.get_transitions("PROJ-42")

        assert result == [
            {"id": "11", "name": "To Do"},
            {"id": "21", "name": "In Progress"},
            {"id": "31", "name": "Done"},
        ]
        args, _ = mock_req.call_args
        assert args[1] == "https://test.atlassian.net/rest/api/3/issue/PROJ-42/transitions"

    def test_empty_transitions(self, client):
        """get_transitions returns empty list when no transitions available."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"transitions": []}

        with patch.object(client._session, "request", return_value=mock_resp):
            result = client.get_transitions("PROJ-1")

        assert result == []


class TestTransitionIssue:
    """Tests for JiraClient.transition_issue."""

    def test_sends_correct_payload(self, client):
        """transition_issue sends the transition ID in the correct format."""
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch.object(client._session, "request", return_value=mock_resp) as mock_req:
            client.transition_issue("PROJ-42", "31")

        args, kwargs = mock_req.call_args
        assert args[0] == "POST"
        assert args[1] == "https://test.atlassian.net/rest/api/3/issue/PROJ-42/transitions"
        assert kwargs["json"] == {"transition": {"id": "31"}}


class TestGetIssue:
    """Tests for JiraClient.get_issue."""

    def test_returns_full_json(self, client):
        """get_issue returns the full issue response as a dict."""
        issue_data = {
            "id": "10001",
            "key": "PROJ-42",
            "fields": {"summary": "Test issue", "status": {"name": "To Do"}},
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = issue_data

        with patch.object(client._session, "request", return_value=mock_resp) as mock_req:
            result = client.get_issue("PROJ-42")

        assert result == issue_data
        args, _ = mock_req.call_args
        assert args[0] == "GET"
        assert args[1] == "https://test.atlassian.net/rest/api/3/issue/PROJ-42"


class TestErrorHandling:
    """Tests for JiraClientError on non-2xx responses."""

    def test_raises_on_404(self, client):
        """Non-2xx responses raise JiraClientError with status code."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.text = '{"errorMessages":["Issue does not exist"]}'

        with patch.object(client._session, "request", return_value=mock_resp):
            with pytest.raises(JiraClientError) as exc_info:
                client.get_issue("PROJ-999")

        err = exc_info.value
        assert err.status_code == 404
        assert "Issue does not exist" in err.body_snippet
        assert "404" in str(err)

    def test_raises_on_401(self, client):
        """401 raises JiraClientError (auth failure scenario)."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch.object(client._session, "request", return_value=mock_resp):
            with pytest.raises(JiraClientError) as exc_info:
                client.create_issue("PROJ", "Title", "Desc")

        assert exc_info.value.status_code == 401

    def test_error_message_never_contains_token(self, client):
        """Exception messages must never leak the API token."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with patch.object(client._session, "request", return_value=mock_resp):
            with pytest.raises(JiraClientError) as exc_info:
                client.get_issue("PROJ-1")

        error_str = str(exc_info.value)
        assert "secret-token-value" not in error_str
        assert client._api_token not in error_str

    def test_body_snippet_truncated(self, client):
        """Long response bodies are truncated in the exception."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "x" * 1000

        with patch.object(client._session, "request", return_value=mock_resp):
            with pytest.raises(JiraClientError) as exc_info:
                client.get_issue("PROJ-1")

        # body_snippet is capped at 500 chars
        assert len(exc_info.value.body_snippet) == 500
        # But the str() representation truncates further to 200 for readability
        assert len(str(exc_info.value)) < 600

    def test_raises_on_create_issue_error(self, client):
        """create_issue properly raises on error response."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 400
        mock_resp.text = '{"errors":{"summary":"Field is required"}}'

        with patch.object(client._session, "request", return_value=mock_resp):
            with pytest.raises(JiraClientError) as exc_info:
                client.create_issue("PROJ", "", "No summary")

        assert exc_info.value.status_code == 400
        assert "summary" in exc_info.value.body_snippet
