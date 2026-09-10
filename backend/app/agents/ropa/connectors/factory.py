"""Builds a live connector from a stored RopaDataSource row.

This is where the two halves of a source configuration are joined: the
NON-SECRET half (host/port/dbname/user/sslmode, or base_url/endpoints) comes
from the database row, and the SECRET half is resolved at call time from the
environment entry named by `credential_ref`.

The secret therefore:
  * is never written to the database
  * is never returned by any API response (the row simply doesn't hold it)
  * is never logged (see `_resolve_secret`, which reports only the ref NAME)
  * is never passed to the LLM (the LLM only ever sees DiscoveryEvidence)

Rotating a credential means changing the environment entry. No DB write, no
re-encryption, no key management.
"""

from __future__ import annotations

import os

from app.agents.ropa.connectors.api import ApiConnectionConfig, ApiConnector, ApiEndpointConfig
from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.postgres import PostgresConnectionConfig, PostgresConnector

# Environment names a source may point at must look like an env var, so a
# malicious/typo'd config can't be used to probe arbitrary process state.
_MAX_CREDENTIAL_REF_LEN = 128


def _resolve_secret(credential_ref: str | None) -> str | None:
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
):
    """Return a ready-to-use SourceConnector for a stored data source."""
    secret = _resolve_secret(credential_ref)

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
