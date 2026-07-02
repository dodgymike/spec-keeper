"""Fernet-based symmetric encryption helper for Jira API tokens.

Provides encrypt/decrypt functions keyed by the JIRA_TOKEN_ENCRYPTION_KEY
environment variable. The key is read at call time (not import time) so tests
can override it via Flask app config or monkeypatching.

Usage::

    from app.crypto import encrypt, decrypt

    ciphertext = encrypt("my-secret-token")
    plaintext = decrypt(ciphertext)
    assert plaintext == "my-secret-token"
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet


class EncryptionKeyMissing(RuntimeError):
    """Raised when JIRA_TOKEN_ENCRYPTION_KEY is not configured."""


class DecryptionError(ValueError):
    """Raised when decryption fails (bad key, corrupted ciphertext, etc.).

    This intentionally does NOT include the ciphertext or plaintext in its
    message to avoid leaking sensitive data in logs or error responses.
    """


def _get_key() -> bytes:
    """Load the Fernet key from the environment at call time."""
    key = os.environ.get("JIRA_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise EncryptionKeyMissing(
            "JIRA_TOKEN_ENCRYPTION_KEY environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return key.encode("utf-8") if isinstance(key, str) else key


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return the ciphertext as a URL-safe
    base64-encoded string.

    Args:
        plaintext: The string to encrypt (e.g. a Jira API token).

    Returns:
        The Fernet ciphertext token as a string.

    Raises:
        EncryptionKeyMissing: If JIRA_TOKEN_ENCRYPTION_KEY is not set.
    """
    key = _get_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string back to plaintext.

    Args:
        ciphertext: A Fernet token string (as returned by encrypt()).

    Returns:
        The original plaintext string.

    Raises:
        EncryptionKeyMissing: If JIRA_TOKEN_ENCRYPTION_KEY is not set.
        DecryptionError: If the ciphertext is invalid, corrupted, or was
            encrypted with a different key.
    """
    key = _get_key()
    f = Fernet(key)
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
