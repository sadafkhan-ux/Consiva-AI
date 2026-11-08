"""Symmetric encryption at rest for secrets a caller hands us.

Used for the Consent Agent API's per-scan callback_secret (migration 0032,
services/agent_webhook_service.py): a value that has to be readable again at
delivery time, but must never be readable from a database dump.

One Fernet master key per deployment (Settings.credential_encryption_key, which
still accepts the old ROPA_CREDENTIAL_ENCRYPTION_KEY name so secrets stored
before this module existed keep decrypting). The key lives only in this
process's environment, never in the database.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretBoxError(Exception):
    """A secret could not be encrypted or decrypted. The message never contains
    the secret or its ciphertext."""


class EncryptionNotConfigured(SecretBoxError):
    """No encryption key is set, so nothing can be encrypted or decrypted.
    Raised rather than silently storing nothing, so a caller who supplied a
    secret learns at once that it cannot be kept."""


def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key
    if not key:
        raise EncryptionNotConfigured(
            "CREDENTIAL_ENCRYPTION_KEY is not set; it is required to store a "
            "callback_secret"
        )
    return Fernet(key.encode("utf-8"))


def encrypt(secret: str) -> str:
    """Returns the ciphertext to store. The plaintext is not retained."""
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # Never include the ciphertext in the message: it is derived from the
        # secret and still does not belong in a log.
        raise SecretBoxError(
            "stored secret could not be decrypted -- CREDENTIAL_ENCRYPTION_KEY "
            "may have changed since it was stored"
        ) from exc
