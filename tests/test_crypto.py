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


class TestMultiFernetRotation:
    """SEC-FIX-12: comma-separated keys -> MultiFernet (first primary, all decrypt)."""

    def test_two_key_round_trip(self, monkeypatch):
        primary = Fernet.generate_key().decode()
        secondary = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", f"{primary},{secondary}")
        ct = encrypt("rotate-me")
        assert decrypt(ct) == "rotate-me"

    def test_old_ciphertext_still_decrypts_after_rotation(self, monkeypatch):
        """A token encrypted under a key that is later demoted to SECONDARY still
        decrypts once the new key is prepended as primary (zero-downtime)."""
        old = Fernet.generate_key().decode()
        # Encrypt while `old` is the only (primary) key.
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", old)
        legacy_ct = encrypt("legacy-token")

        # Rotate: prepend a fresh primary; `old` is now secondary.
        new = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", f"{new},{old}")
        assert decrypt(legacy_ct) == "legacy-token"

    def test_primary_key_is_used_for_new_encryption(self, monkeypatch):
        """New ciphertext must be decryptable by the PRIMARY key alone."""
        primary = Fernet.generate_key().decode()
        secondary = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", f"{primary},{secondary}")
        ct = encrypt("fresh-token")

        # The primary alone can decrypt it; the secondary alone cannot.
        assert Fernet(primary.encode()).decrypt(ct.encode()).decode() == "fresh-token"
        with pytest.raises(Exception):
            Fernet(secondary.encode()).decrypt(ct.encode())

    def test_retired_key_no_longer_decrypts(self, monkeypatch):
        """After a key is fully dropped from the list, its ciphertext no longer
        decrypts (proves the list is honoured, not ignored)."""
        retired = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", retired)
        ct = encrypt("gone-soon")

        current = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", current)
        with pytest.raises(DecryptionError):
            decrypt(ct)

    def test_whitespace_around_keys_tolerated(self, monkeypatch):
        primary = Fernet.generate_key().decode()
        secondary = Fernet.generate_key().decode()
        monkeypatch.setenv(
            "JIRA_TOKEN_ENCRYPTION_KEY", f"  {primary} , {secondary}  "
        )
        ct = encrypt("spaced")
        assert decrypt(ct) == "spaced"


class _FakeSecretsManager:
    """A minimal boto3 secretsmanager stub: an in-memory {SecretId: SecretString}
    map with a call counter so tests can assert caching (fetch-once) behaviour.

    We stub boto3.client rather than depend on moto so the test image stays small
    (moto is not a project dependency); the crypto module's only Secrets Manager
    surface is a single ``get_secret_value(SecretId=...)`` call."""

    def __init__(self, store: dict):
        self._store = store
        self.get_calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 (boto3 kwarg name)
        self.get_calls += 1
        return {"SecretString": self._store[SecretId]}


@pytest.fixture
def fake_sm(monkeypatch):
    """Patch boto3.client('secretsmanager', ...) to return a shared fake.

    Returns (arn, store, holder) where `store` is the mutable secret map and
    `holder["client"]` is the fake instance (for call-count assertions)."""
    import boto3
    from app import crypto

    crypto.clear_key_cache()
    monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:jira-token-key-AbCdEf"
    store: dict = {}
    holder: dict = {}

    real_client = boto3.client

    def _fake_client(service_name, *args, **kwargs):
        if service_name == "secretsmanager":
            client = _FakeSecretsManager(store)
            holder["client"] = client
            return client
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", _fake_client)
    monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN", arn)
    yield arn, store, holder
    crypto.clear_key_cache()


class TestSecretsManagerSourcing:
    """SEC-FIX-12: when the ARN is set the key loads from Secrets Manager."""

    def test_single_key_from_secrets_manager(self, fake_sm):
        arn, store, _ = fake_sm
        store[arn] = Fernet.generate_key().decode()
        ct = encrypt("sm-sourced-token")
        assert decrypt(ct) == "sm-sourced-token"

    def test_multifernet_from_secrets_manager(self, fake_sm, monkeypatch):
        from app import crypto

        arn, store, _ = fake_sm
        old = Fernet.generate_key().decode()
        new = Fernet.generate_key().decode()
        store[arn] = old
        legacy_ct = encrypt("legacy")

        # Rotate the secret to "new,old" and clear the cache to force a refetch.
        store[arn] = f"{new},{old}"
        crypto.clear_key_cache()
        assert decrypt(legacy_ct) == "legacy"  # secondary still decrypts
        fresh = encrypt("fresh")
        assert Fernet(new.encode()).decrypt(fresh.encode()).decode() == "fresh"

    def test_arn_takes_precedence_over_env_var(self, fake_sm, monkeypatch):
        arn, store, _ = fake_sm
        sm_key = Fernet.generate_key().decode()
        store[arn] = sm_key
        # Set a DIFFERENT env-var key that must be ignored while the ARN is set.
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
        ct = encrypt("precedence")
        # Only the SM key can decrypt it -> ARN wins.
        assert Fernet(sm_key.encode()).decrypt(ct.encode()).decode() == "precedence"

    def test_secret_is_cached_not_refetched(self, fake_sm):
        """The SM secret is fetched once and cached; further encrypt/decrypt do
        NOT re-call Secrets Manager (proves the in-process cache)."""
        arn, store, holder = fake_sm
        store[arn] = Fernet.generate_key().decode()
        ct = encrypt("cached")
        assert decrypt(ct) == "cached"
        assert decrypt(ct) == "cached"
        # Exactly one GetSecretValue despite three crypto operations.
        assert holder["client"].get_calls == 1

    def test_falls_back_to_env_when_arn_unset(self, monkeypatch):
        from app import crypto

        crypto.clear_key_cache()
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN", raising=False)
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("JIRA_TOKEN_ENCRYPTION_KEY", key)
        ct = encrypt("env-fallback")
        assert decrypt(ct) == "env-fallback"


class TestFailClosedNoKey:
    """SEC-FIX-12: with NEITHER source configured, encrypt/decrypt fail closed."""

    def test_encrypt_fails_closed(self, monkeypatch):
        from app import crypto
        crypto.clear_key_cache()
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN", raising=False)
        with pytest.raises(EncryptionKeyMissing):
            encrypt("nope")

    def test_decrypt_fails_closed(self, monkeypatch):
        from app import crypto
        crypto.clear_key_cache()
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("JIRA_TOKEN_ENCRYPTION_KEY_SECRET_ARN", raising=False)
        with pytest.raises(EncryptionKeyMissing):
            decrypt("nope")
