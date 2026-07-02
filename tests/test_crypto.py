"""Tests for app.crypto — Fernet-based token encryption helper (JIRA-2).

These tests verify:
1. Round-trip: encrypt then decrypt returns the original plaintext.
2. Non-determinism: encrypting the same plaintext twice produces different ciphertexts
   (Fernet uses a random IV per encryption).
3. Bad-input handling: decrypting garbage raises DecryptionError (not a raw exception,
   and the error message does not leak plaintext or ciphertext).
4. Missing-key handling: operations raise EncryptionKeyMissing when the env var is unset.
5. Wrong-key handling: decrypting with a different key raises DecryptionError.

NOTE: These are pure unit tests with NO database dependency. The conftest.py
autouse fixtures (app, _clean_tables) are overridden here to avoid needing a
running PostgreSQL instance.
"""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from app.crypto import DecryptionError, EncryptionKeyMissing, decrypt, encrypt


# Override the session-scoped conftest fixtures that require PostgreSQL.
# These tests are pure crypto unit tests with no DB dependency.
@pytest.fixture
def app():
    """No-op override: crypto tests don't need a Flask app or DB."""
    return None


@pytest.fixture(autouse=True)
def _clean_tables():
    """No-op override: crypto tests don't touch the database."""
    yield


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Provide a valid Fernet key for all tests in this module."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)


class TestRoundTrip:
    """encrypt() -> decrypt() returns the original plaintext."""

    def test_basic_round_trip(self):
        original = "my-super-secret-jira-token-12345"
        ciphertext = encrypt(original)
        assert decrypt(ciphertext) == original

    def test_empty_string_round_trip(self):
        original = ""
        ciphertext = encrypt(original)
        assert decrypt(ciphertext) == original

    def test_unicode_round_trip(self):
        original = "token-with-unicode-☃-❤"
        ciphertext = encrypt(original)
        assert decrypt(ciphertext) == original

    def test_long_token_round_trip(self):
        original = "x" * 10000
        ciphertext = encrypt(original)
        assert decrypt(ciphertext) == original


class TestCiphertextNonDeterminism:
    """Fernet uses a random IV, so the same plaintext produces different ciphertexts."""

    def test_different_ciphertexts_for_same_plaintext(self):
        plaintext = "repeated-token"
        ct1 = encrypt(plaintext)
        ct2 = encrypt(plaintext)
        assert ct1 != ct2, "Same plaintext should produce different ciphertexts"

    def test_both_ciphertexts_decrypt_correctly(self):
        plaintext = "repeated-token"
        ct1 = encrypt(plaintext)
        ct2 = encrypt(plaintext)
        assert decrypt(ct1) == plaintext
        assert decrypt(ct2) == plaintext

    def test_different_plaintexts_produce_different_ciphertexts(self):
        ct1 = encrypt("token-alpha")
        ct2 = encrypt("token-beta")
        assert ct1 != ct2


class TestBadInput:
    """Decrypting invalid data raises DecryptionError cleanly."""

    def test_garbage_input(self):
        with pytest.raises(DecryptionError) as exc_info:
            decrypt("not-a-valid-fernet-token")
        # Error message must NOT contain the garbage input
        assert "not-a-valid-fernet-token" not in str(exc_info.value)

    def test_empty_ciphertext(self):
        with pytest.raises(DecryptionError):
            decrypt("")

    def test_truncated_ciphertext(self):
        ciphertext = encrypt("hello")
        # Truncate the ciphertext
        with pytest.raises(DecryptionError):
            decrypt(ciphertext[:10])

    def test_modified_ciphertext(self):
        ciphertext = encrypt("hello")
        # Flip a character in the middle
        chars = list(ciphertext)
        mid = len(chars) // 2
        chars[mid] = "A" if chars[mid] != "A" else "B"
        modified = "".join(chars)
        with pytest.raises(DecryptionError):
            decrypt(modified)

    def test_error_does_not_leak_plaintext(self):
        """Even if we somehow get a weird error, plaintext must not appear."""
        with pytest.raises(DecryptionError) as exc_info:
            decrypt("some-secret-looking-garbage-data")
        error_msg = str(exc_info.value)
        assert "plaintext" not in error_msg.lower() or "the ciphertext" in error_msg.lower()


class TestMissingKey:
    """Operations fail cleanly when JIRA_TOKEN_ENCRYPTION_KEY is not set."""

    def test_encrypt_without_key(self, monkeypatch):
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionKeyMissing):
            encrypt("some-token")

    def test_decrypt_without_key(self, monkeypatch):
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionKeyMissing):
            decrypt("some-ciphertext")


class TestWrongKey:
    """Decrypting with a different key than was used to encrypt raises DecryptionError."""

    def test_wrong_key_raises(self, monkeypatch):
        # Encrypt with key A
        key_a = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key_a)
        ciphertext = encrypt("secret-data")

        # Try to decrypt with key B
        key_b = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key_b)
        with pytest.raises(DecryptionError):
            decrypt(ciphertext)
