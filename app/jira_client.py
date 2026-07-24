"""Thin Jira Cloud REST API v3 client.

Provides a typed wrapper around Jira Cloud's REST API using HTTP Basic
authentication (email + API token) as required by Atlassian Cloud.

No retry logic lives here — that belongs in the sync service (JIRA-8).
"""
from __future__ import annotations

import requests


class JiraClientError(Exception):
    """Raised when the Jira API returns a non-2xx response.

    Attributes:
        status_code: The HTTP status code from Jira.
        body_snippet: First 500 chars of the response body for debuggability.
    """

    def __init__(self, status_code: int, body_snippet: str, method: str, url: str):
        self.status_code = status_code
        self.body_snippet = body_snippet
        # Intentionally do NOT include auth credentials in the message.
        super().__init__(
            f"Jira API error: {method} {url} returned {status_code}: "
            f"{body_snippet[:200]}"
        )


class JiraClient:
    """Minimal Jira Cloud REST API v3 client.

    Args:
        base_url: The Jira Cloud instance URL (e.g. "https://myorg.atlassian.net").
        email: The Atlassian account email for Basic auth.
        api_token: The Atlassian API token (never logged or included in exceptions).
    """

    API_PATH = "/rest/api/3"

    def __init__(self, base_url: str, email: str, api_token: str):
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._session = requests.Session()
        self._session.auth = (email, api_token)
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _url(self, path: str) -> str:
        return f"{self._base_url}{self.API_PATH}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Send a request and raise JiraClientError on non-2xx responses."""
        url = self._url(path)
        resp = self._session.request(method, url, **kwargs)
        if not resp.ok:
            # Truncate body to avoid leaking large payloads into logs.
            # Never include auth credentials in exception messages.
            body_snippet = resp.text[:500] if resp.text else ""
            raise JiraClientError(
                status_code=resp.status_code,
                body_snippet=body_snippet,
                method=method.upper(),
                url=url,
            )
        return resp

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
    ) -> str:
        """Create an issue in Jira and return the created issue key.

        Args:
            project_key: The Jira project key (e.g. "PROJ").
            summary: Issue summary/title.
            description: Plain-text description (sent as ADF paragraph).
            issue_type: Issue type name (default "Task").

        Returns:
            The issue key (e.g. "PROJ-123").
        """
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": description}],
                        }
                    ],
                },
                "issuetype": {"name": issue_type},
            }
        }
        resp = self._request("POST", "/issue", json=payload)
        data = resp.json()
        return data["key"]

    def get_issue(self, issue_key: str) -> dict:
        """Fetch a single issue by key.

        Returns:
            The full issue JSON response as a dict.
        """
        resp = self._request("GET", f"/issue/{issue_key}")
        return resp.json()

    def get_transitions(self, issue_key: str) -> list[dict]:
        """Get available workflow transitions for an issue.

        Returns:
            A list of dicts, each with at least "id" and "name" keys.
        """
        resp = self._request("GET", f"/issue/{issue_key}/transitions")
        data = resp.json()
        return [
            {"id": t["id"], "name": t["name"]}
            for t in data.get("transitions", [])
        ]

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        """Transition an issue to a new workflow state.

        Args:
            issue_key: The issue key (e.g. "PROJ-123").
            transition_id: The transition ID (from get_transitions).
        """
        payload = {"transition": {"id": transition_id}}
        self._request("POST", f"/issue/{issue_key}/transitions", json=payload)
