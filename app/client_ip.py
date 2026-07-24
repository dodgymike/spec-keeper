"""Origin-locked client-IP resolution for the PUBLIC (unauthenticated) surface.

The signup and enrollment endpoints key their per-IP rate limiter on the caller
IP. Behind Cloudflare the true client IP arrives in ``CF-Connecting-IP`` (a single
value Cloudflare sets) rather than the immediate TCP peer. But those forwarding
headers are only trustworthy when the request is GUARANTEED to have transited
Cloudflare — i.e. when the origin-lock gate is effectively ENFORCING (SEC-EDGE-1):
``ORIGIN_LOCK_MODE == "enforce"`` with a non-empty ``ORIGIN_LOCK_SECRET``. On the
raw ``execute-api`` host (origin-lock off/warn, or no secret) an attacker can
forge/rotate ``CF-Connecting-IP``/``X-Forwarded-For`` per request to defeat the
per-IP floor, so we must ignore them and key on the real peer (``remote_addr``).

SEC-FIX-5 factored this out of ``signup._client_ip`` so signup + enroll share ONE
policy. ``X-Forwarded-For`` is deliberately NOT consulted even when enforcing: it
is a client-appendable list, whereas ``CF-Connecting-IP`` is a single value set by
the trusted edge.
"""
from __future__ import annotations

from flask import current_app, request


def _origin_lock_enforcing(cfg) -> bool:
    """True iff the origin-lock gate is EFFECTIVELY enforcing — mode ``enforce``
    AND a non-empty secret. Mirrors the degrade-to-off rule in
    ``create_app._register_origin_lock`` (an empty secret disables the gate, so the
    forwarding headers are NOT trustworthy even if the mode says ``enforce``)."""
    mode = (cfg.get("ORIGIN_LOCK_MODE") or "off").strip().lower()
    secret = cfg.get("ORIGIN_LOCK_SECRET") or ""
    return mode == "enforce" and bool(secret)


def client_ip() -> str:
    """Resolve the client IP used to key the public per-IP rate limiter.

    Only when origin-lock is effectively enforcing do we trust Cloudflare's
    ``CF-Connecting-IP`` (a single edge-set value) over the peer address; otherwise
    the forwarding headers are attacker-controllable, so we fall back to
    ``request.remote_addr``. ``X-Forwarded-For`` is never used as a source (it is a
    client-appendable list)."""
    if _origin_lock_enforcing(current_app.config):
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
    return request.remote_addr or ""
