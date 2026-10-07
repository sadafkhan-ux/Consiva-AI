"""Unit tests for the optional encrypted-at-rest ROPA credential path
(migration 0031, connectors/factory.py). Pure crypto + settings -- no
database needed, unlike the env-var `credential_ref` path's connector tests.
"""

import pytest
from cryptography.fernet import Fernet

from app.agents.ropa.connectors import factory
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    """get_settings() is process-wide @lru_cache'd, so a key set by one test
    would otherwise leak into every test that runs after it in this process."""
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_encrypt_then_decrypt_roundtrips(monkeypatch):
    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    ciphertext = factory.encrypt_credential("hunter2-but-a-real-db-password")
    assert "hunter2" not in ciphertext, "ciphertext must not contain the plaintext"
    assert factory._decrypt_credential(ciphertext) == "hunter2-but-a-real-db-password"


def test_decryption_fails_closed_with_the_wrong_key(monkeypatch):
    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    ciphertext = factory.encrypt_credential("a-secret")

    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(Exception) as excinfo:
        factory._decrypt_credential(ciphertext)
    # Never lets the ciphertext or any derived value leak into the message.
    assert ciphertext not in str(excinfo.value)


def test_encrypt_refuses_when_no_key_is_configured(monkeypatch):
    monkeypatch.delenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(factory.CredentialEncryptionNotConfigured):
        factory.encrypt_credential("a-secret")


def test_build_connector_prefers_ciphertext_over_credential_ref(monkeypatch):
    """The whole point of the fallback order: a source migrated to the
    encrypted path must not keep reading a stale env-var secret."""
    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SOME_STALE_ENV_SECRET", "stale-password")
    get_settings.cache_clear()
    ciphertext = factory.encrypt_credential("current-password")

    connector = factory.build_connector(
        connector="postgres",
        config={"host": "db.example.com", "dbname": "app", "user": "ro"},
        credential_ref="SOME_STALE_ENV_SECRET",
        credential_ciphertext=ciphertext,
    )
    assert connector._config.password == "current-password"


def test_build_connector_falls_back_to_credential_ref_when_no_ciphertext_stored(monkeypatch):
    """A source that has never used the encrypted path must behave exactly
    as it did before this feature existed."""
    monkeypatch.setenv("SOME_ENV_SECRET", "env-password")
    get_settings.cache_clear()

    connector = factory.build_connector(
        connector="postgres",
        config={"host": "db.example.com", "dbname": "app", "user": "ro"},
        credential_ref="SOME_ENV_SECRET",
        credential_ciphertext=None,
    )
    assert connector._config.password == "env-password"


def test_a_corrupted_stored_credential_raises_a_clear_connector_error(monkeypatch):
    """A deployment that rotated its master key must get a clear error at
    connect time, never a silent auth failure against the wrong password."""
    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    from app.agents.ropa.connectors.base import ConnectorError

    with pytest.raises(ConnectorError):
        factory.build_connector(
            connector="postgres",
            config={"host": "db.example.com", "dbname": "app", "user": "ro"},
            credential_ref=None,
            credential_ciphertext="not-a-real-fernet-token",
        )
