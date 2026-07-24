"""Jira base-URL safety validation (SEC-FIX-1).

Single source of truth for the SSRF guard applied to a Jira integration's
``base_url``. The server calls that URL server-side (transition-cache warmup on
create/enable, and on every task completion), so an unvalidated value is an
authenticated SSRF primitive (internal IPs, cloud metadata, etc.).

The SAME check is enforced in TWO places, defense-in-depth:
  1. The Marshmallow schemas (``JiraConfigIn`` / ``JiraConfigUpdate``) — rejects
     a bad value at the API boundary with a 422.
  2. :class:`app.jira_client.JiraClient` — a value that somehow bypasses the
     schema still cannot reach an internal host.

Rules (all must hold):
  * scheme MUST be ``https``;
  * no embedded credentials (``user:pass@host``);
  * host MUST NOT be an IP literal in a private/loopback/link-local/unique-local/
    reserved/unspecified/multicast range, nor ``localhost``;
  * only the default https port (443) or no explicit port;
  * host MUST match the configured allow-list of suffixes (default
    ``.atlassian.net`` — Jira Cloud), overridable via ``JIRA_ALLOWED_HOST_SUFFIXES``
    so a self-hosted Jira can be permitted deliberately.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: Default host allow-list — Jira Cloud tenants. Overridable per-deployment via
#: the ``JIRA_ALLOWED_HOST_SUFFIXES`` config var (comma-separated).
DEFAULT_ALLOWED_HOST_SUFFIXES = (".atlassian.net",)


class JiraUrlError(ValueError):
    """Raised when a Jira ``base_url`` fails the SSRF/allow-list validation."""


def _normalize_suffixes(suffixes) -> tuple[str, ...]:
    """Lower-case, dot-prefix and de-dup the configured suffixes."""
    out: list[str] = []
    for s in suffixes or ():
        s = str(s).strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        if s not in out:
            out.append(s)
    return tuple(out) or DEFAULT_ALLOWED_HOST_SUFFIXES


def _is_forbidden_ip(host: str) -> bool:
    """True when ``host`` is an IP literal in a non-routable/internal range."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP literal — the allow-list still applies
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def validate_jira_base_url(url, allowed_suffixes=DEFAULT_ALLOWED_HOST_SUFFIXES) -> str:
    """Validate a Jira ``base_url``; return it unchanged or raise ``JiraUrlError``."""
    if not isinstance(url, str) or not url.strip():
        raise JiraUrlError("base_url is required.")

    parts = urlsplit(url.strip())

    if parts.scheme != "https":
        raise JiraUrlError("base_url must use https://.")

    # Reject embedded credentials (userinfo@host).
    if parts.username or parts.password or "@" in parts.netloc:
        raise JiraUrlError("base_url must not contain embedded credentials.")

    host = parts.hostname  # already lower-cased, brackets stripped for IPv6
    if not host:
        raise JiraUrlError("base_url must include a host.")
    host = host.lower()

    if host == "localhost" or host.endswith(".localhost"):
        raise JiraUrlError("base_url host is not allowed.")

    if _is_forbidden_ip(host):
        raise JiraUrlError(
            "base_url host is not allowed (private/loopback/link-local address)."
        )

    # Only the default https port (or none) — blocks reaching odd internal ports
    # on an otherwise-allowed host.
    try:
        port = parts.port
    except ValueError:
        raise JiraUrlError("base_url has an invalid port.")
    if port is not None and port != 443:
        raise JiraUrlError("base_url must use the default https port (443).")

    suffixes = _normalize_suffixes(allowed_suffixes)
    if not any(host == s.lstrip(".") or host.endswith(s) for s in suffixes):
        raise JiraUrlError(
            "base_url host is not in the allowed Jira host allow-list."
        )

    return url.strip()
