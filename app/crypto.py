"""Fernet-based symmetric encryption helper for Jira API tokens.

Provides encrypt/decrypt functions for a project's stored Jira API token. The
key material is sourced (at call time, so tests can monkeypatch it) from one of
two places, in precedence order:

1. ``JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN`` — when set, the key material is
   loaded ONCE from that AWS Secrets Manager secret (CMK-encrypted, mirroring
   ``agent-credentials``) and cached in-process. This is the production bar:
   the key is never a bare environment variable on the Lambda.
2. ``JIRA_TOKEN_ENCRYPTION_KEY`` — the direct value, used as the local/dev
   fallback when no ARN is configured.

Either source may hold a SINGLE Fernet key or a COMMA-SEPARATED list. With a
list the FIRST key is the PRIMARY (used for all new encryption) and EVERY key
can decrypt (``cryptography``'s :class:`MultiFernet`). This is what enables a
zero-downtime key rotation: prepend the new key (it becomes primary), let old
ciphertext decrypt under the now-secondary key, re-encrypt lazily, then drop the
retired key from the list.

The key material is NEVER logged or included in any exception message.

Usage::

    from app.crypto import encrypt, decrypt

    ciphertext = encrypt("my-secret-token")
    plaintext = decrypt(ciphertext)
    assert plaintext == "my-secret-token"
"""
from __future__ import annotations

import os
import threading

from cryptography.fernet import Fernet, MultiFernet

_ENV_KEY = "JIRA_TOKEN_ENCRYPTION_KEY"
_ENV_SECRET_ARN = "JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN"

# In-process cache of key material fetched from Secrets Manager, keyed by ARN so a
# changed ARN re-fetches. Guarded by a lock so concurrent first-use is safe. Only
# the (already-secret-at-rest) key STRING is held; it is never logged.
_secret_cache: dict[str, str] = {}
_secret_cache_lock = threading.Lock()


class EncryptionKeyMissing(RuntimeError):
    """Raised when no Jira token encryption key is configured (neither the
    Secrets Manager ARN nor the direct env var)."""


class DecryptionError(ValueError):
    """Raised when decryption fails (bad key, corrupted ciphertext, etc.).

    This intentionally does NOT include the ciphertext or plaintext in its
    message to avoid leaking sensitive data in logs or error responses.
    """


def clear_key_cache() -> None:
    """Drop the cached Secrets Manager key material (for tests / after rotation)."""
    with _secret_cache_lock:
        _secret_cache.clear()


def _load_from_secrets_manager(arn: str) -> str:
    """Fetch (and cache) the key material string from Secrets Manager by ARN.

    The secret's ``SecretString`` is the raw key material (a single Fernet key or
    a comma-separated list), matching the direct env var's format. Never logged.
    """
    cached = _secret_cache.get(arn)
    if cached is not None:
        return cached
    with _secret_cache_lock:
        cached = _secret_cache.get(arn)
        if cached is not None:
            return cached
        import boto3  # lazy: keep boto3 off the import path when Jira is unused

        kwargs = {}
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            kwargs["region_name"] = region
        client = boto3.client("secretsmanager", **kwargs)
        resp = client.get_secret_value(SecretId=arn)
        material = resp.get("SecretString")
        if not material:
            raise EncryptionKeyMissing(
                f"Secrets Manager secret referenced by {_ENV_SECRET_ARN} "
                "has no SecretString value."
            )
        _secret_cache[arn] = material
        return material


def _key_material() -> str:
    """Return the raw key-material string from Secrets Manager (if an ARN is set)
    else the direct env var. Raises EncryptionKeyMissing when neither is set."""
    arn = os.environ.get(_ENV_SECRET_ARN)
    if arn:
        return _load_from_secrets_manager(arn)
    material = os.environ.get(_ENV_KEY)
    if not material:
        raise EncryptionKeyMissing(
            f"No Jira token encryption key configured: set {_ENV_SECRET_ARN} "
            f"(Secrets Manager, production) or {_ENV_KEY} (local/dev). "
            "Generate a key with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        )
    return material


def _get_fernet() -> MultiFernet:
    """Build a MultiFernet from the configured key material.

    The material is split on commas into one or more Fernet keys; the FIRST is
    primary (encrypts), all can decrypt. A single key yields a one-element
    MultiFernet, preserving prior single-key behaviour.
    """
    material = _key_material()
    keys = [k.strip() for k in material.split(",") if k.strip()]
    if not keys:
        raise EncryptionKeyMissing(
            "Jira token encryption key material is empty after parsing."
        )
    try:
        fernets = [Fernet(k.encode("utf-8")) for k in keys]
    except Exception as exc:
        # A malformed key must not leak its (partial) material in the message.
        raise EncryptionKeyMissing(
            "Jira token encryption key material is not a valid Fernet key."
        ) from exc
    return MultiFernet(fernets)


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return the ciphertext as a URL-safe
    base64-encoded string.

    Args:
        plaintext: The string to encrypt (e.g. a Jira API token).

    Returns:
        The Fernet ciphertext token as a string (encrypted under the PRIMARY key).

    Raises:
        EncryptionKeyMissing: If no encryption key is configured.
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string back to plaintext.

    Any key in the configured MultiFernet may have produced the ciphertext, so a
    token written under a now-secondary (rotated-out-of-primary) key still
    decrypts.

    Args:
        ciphertext: A Fernet token string (as returned by encrypt()).

    Returns:
        The original plaintext string.

    Raises:
        EncryptionKeyMissing: If no encryption key is configured.
        DecryptionError: If the ciphertext is invalid, corrupted, or was
            encrypted with a key not in the configured set.
    """
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        # Broad catch is intentional at this security boundary: we must never
        # let an unexpected error propagate raw details (key material, partial
        # plaintext) to callers. All failures surface as a single safe message.
        raise DecryptionError(
            "Decryption failed: the ciphertext is invalid or was encrypted "
            "with a different key."
        ) from exc
