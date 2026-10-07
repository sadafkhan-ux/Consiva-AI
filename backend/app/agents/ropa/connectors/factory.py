"""Builds a live connector from a stored RopaDataSource row.

This is where the two halves of a source configuration are joined: the
NON-SECRET half (host/port/dbname/user/sslmode, or base_url/endpoints) comes
from the database row, and the SECRET half is resolved at call time from
EITHER of two places (see `_resolve_secret`):

  1. `credential_ciphertext` -- an encrypted secret stored on the row itself
     (migration 0031), set via POST /sources/{id}/credential. Tried first.
  2. `credential_ref` -- the NAME of an environment entry holding the secret,
     resolved by `os.getenv`. The original mechanism; still fully supported,
     and what every source uses when ROPA_CREDENTIAL_ENCRYPTION_KEY is unset.

The secret therefore:
  * is never written to the database in PLAINTEXT (ciphertext only, under a
    master key that itself lives only in this process's environment)
  * is never returned by any API response (DataSourceResponse has no secret
    field, ciphertext included)
  * is never logged (see `_resolve_secret`, which reports only the ref NAME
    or the fact that a stored credential failed to decrypt -- never a value)
  * is never passed to the LLM (the LLM only ever sees DiscoveryEvidence)

Rotating an env-var-backed credential means changing the environment entry --
no DB write, no re-encryption. Rotating an encrypted one means calling
POST /sources/{id}/credential again; the old ciphertext is simply overwritten.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from app.agents.ropa.connectors.api import ApiConnectionConfig, ApiConnector, ApiEndpointConfig
from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.postgres import PostgresConnectionConfig, PostgresConnector
from app.config import get_settings

# Environment names a source may point at must look like an env var, so a
# malicious/typo'd config can't be used to probe arbitrary process state.
_MAX_CREDENTIAL_REF_LEN = 128


class CredentialEncryptionNotConfigured(ConnectorError):
    """ROPA_CREDENTIAL_ENCRYPTION_KEY is unset, so neither encrypting a new
    secret nor decrypting a previously-stored one is possible. Raised instead
    of silently doing nothing, because a stored credential a deployment can no
    longer decrypt is a discovery that will fail obscurely at connect time if
    this isn't surfaced clearly first."""


def _fernet() -> Fernet:
    key = get_settings().ropa_credential_encryption_key
    if not key:
        raise CredentialEncryptionNotConfigured(
            "ROPA_CREDENTIAL_ENCRYPTION_KEY is not set; set it to use "
            "POST /sources/{id}/credential, or configure this source with "
            "credential_ref instead"
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionNotConfigured(
            "ROPA_CREDENTIAL_ENCRYPTION_KEY is set but is not a valid Fernet key "
            "(expected 32 url-safe base64-encoded bytes)"
        ) from exc


def encrypt_credential(secret: str) -> str:
    """For POST /sources/{id}/credential. Returns the ciphertext to store;
    the plaintext `secret` is never retained by this function's caller after
    this call -- see the route, which does not log or echo it.

    Also reused by app/services/agent_webhook_service.py for a per-run/per-scan
    callback secret override (migration 0032): one Fernet master key per
    deployment for anything sensitive at rest is simpler than a key per use
    case, and the threat model is identical -- a secret a caller hands us that
    must never be readable from a database dump."""
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_credential(ciphertext: str) -> str:
    """Public form of `_decrypt_credential` for callers outside this module
    (see `encrypt_credential`'s docstring for why they share one key)."""
    return _decrypt_credential(ciphertext)


def _decrypt_credential(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # Never include the ciphertext in the message -- it's not the secret
        # itself, but it's derived from it and still doesn't belong in a log.
        raise ConnectorError(
            "stored credential could not be decrypted -- ROPA_CREDENTIAL_ENCRYPTION_KEY "
            "may have changed since it was set; re-run POST /sources/{id}/credential"
        ) from exc


def _resolve_secret(
    credential_ref: str | None,
    credential_ciphertext: str | None = None,
    raw_secret: str | None = None,
) -> str | None:
    """Precedence: `raw_secret` (a value the caller holds directly -- the ONLY
    case this is used is testing a connection before a source has even been
    saved, see routes/ropa.py's test_connection; it is never persisted by
    this function or its caller) > `credential_ciphertext` (0031) >
    `credential_ref` (the original env-var path)."""
    if raw_secret:
        return raw_secret
    if credential_ciphertext:
        return _decrypt_credential(credential_ciphertext)
    if not credential_ref:
        return None
    if len(credential_ref) > _MAX_CREDENTIAL_REF_LEN or not credential_ref.replace("_", "").isalnum():
        raise ConnectorError(f"invalid credential_ref format: {credential_ref!r}")
    secret = os.getenv(credential_ref)
    if secret is None:
        # Names the REF, never a value -- this message reaches logs and API errors.
        raise ConnectorError(
            f"credential_ref {credential_ref!r} is not set in this environment; "
            "configure the secret before running discovery"
        )
    return secret


def build_connector(
    *,
    connector: str,
    config: dict,
    credential_ref: str | None,
    credential_ciphertext: str | None = None,
    raw_secret: str | None = None,
):
    """Return a ready-to-use SourceConnector. `raw_secret` is for testing a
    connection before a source row exists at all (routes/ropa.py's
    POST /sources/test-connection) -- every other caller omits it and gets the
    stored-row behavior (ciphertext, then credential_ref)."""
    secret = _resolve_secret(credential_ref, credential_ciphertext, raw_secret)

    if connector == "postgres":
        missing = [k for k in ("host", "dbname", "user") if not config.get(k)]
        if missing:
            raise ConnectorError(f"postgres source config missing: {missing}")
        if secret is None:
            raise ConnectorError("postgres source requires a credential_ref for its password")
        return PostgresConnector(
            PostgresConnectionConfig(
                host=config["host"],
                port=int(config.get("port", 5432)),
                dbname=config["dbname"],
                user=config["user"],
                password=secret,
                sslmode=config.get("sslmode", "require"),
                schemas=tuple(config["schemas"]) if config.get("schemas") else None,
                collect_sample_patterns=bool(config.get("collect_sample_patterns", False)),
                connect_timeout_seconds=float(config.get("connect_timeout_seconds", 5.0)),
                allow_superuser=bool(config.get("allow_superuser", False)),
            )
        )

    if connector == "rest_api":
        if not config.get("base_url"):
            raise ConnectorError("rest_api source config missing: base_url")
        endpoints = config.get("endpoints") or []
        if not endpoints:
            raise ConnectorError("rest_api source config missing: endpoints")
        return ApiConnector(
            ApiConnectionConfig(
                base_url=config["base_url"],
                endpoints=tuple(
                    ApiEndpointConfig(
                        name=e["name"], path=e["path"], records_key=e.get("records_key")
                    )
                    for e in endpoints
                ),
                api_key=secret,
                auth_scheme=config.get("auth_scheme", "Bearer"),
                extra_headers=config.get("extra_headers", {}),
                timeout_seconds=float(config.get("timeout_seconds", 15.0)),
                collect_sample_patterns=bool(config.get("collect_sample_patterns", False)),
                verify_tls=bool(config.get("verify_tls", True)),
            )
        )

    raise ConnectorError(f"unsupported connector {connector!r}")
