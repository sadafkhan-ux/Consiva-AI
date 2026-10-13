"""Builds a live DSR connector from a stored source + its DSR authorization.

Same secret discipline as Agent 2's factory (agents/ropa/connectors/factory.py): the
non-secret half of the connection comes from the `ropa_data_sources.config` row, and
the secret half is resolved at call time. The secret is never stored, never returned
by an API, never logged and never reaches the LLM.

The READ credential is resolved from the SAME `ropa_data_sources` row Agent 2 already
reads, so it supports both of that row's credential mechanisms, exactly as Agent 2's
own factory does: `credential_ciphertext` (migration 0031, set via
POST /sources/{id}/credential and decrypted with Agent 2's Fernet key) tried first,
falling back to `credential_ref` (the NAME of an environment entry). A source an
administrator set up with only a ciphertext -- no credential_ref env var -- discovers
fine under Agent 2, so it must resolve just as well here; otherwise DSR would fail
every such source with a misleading "no credential_ref" error for a credential that
in fact exists and resolves.

The WRITE credential has no ciphertext counterpart: `dsr_source_authorizations.
write_credential_ref` is DSR's own column (no `write_credential_ciphertext` field
exists), so it resolves only via the environment-entry path.

What is different here, and deliberately so: a DSR connector may be built in two
modes. `for_search` resolves only the READ credential. `for_execution` additionally
resolves the WRITE credential named by the DSR authorization, and refuses to build
at all unless the authorization actually permits execution. A connector built for
search therefore cannot write even if something later asks it to -- it has no write
password to open a connection with.
"""

from __future__ import annotations

import os
from typing import Any

from app.agents.ropa.connectors.base import ConnectorError as RopaConnectorError
from app.agents.ropa.connectors.factory import decrypt_credential

from app.agents.dsr.connectors.authorization import SourceGrant, resolve
from app.agents.dsr.connectors.postgres import PostgresDsrConfig, PostgresDsrConnector
from app.agents.dsr.errors import SourceNotAuthorizedError

# The single source of truth for "can DSR actually search this source at all" --
# build_connector's own if/elif below is the only code that would otherwise encode
# this, which meant it was only discoverable by attempting a search and watching it
# fail. Exposed here so the config-time authorization route (PUT /config/sources)
# can reject an unsupported connector up front, instead of accepting an authorization
# that is guaranteed to fail every time a search actually runs against it.
SUPPORTED_CONNECTORS = frozenset({"postgres"})

_MAX_CREDENTIAL_REF_LEN = 128


def _resolve_secret(
    credential_ref: str | None, *, purpose: str, credential_ciphertext: str | None = None,
) -> str | None:
    """Read a secret by the NAME the configuration gives, or decrypt it if the
    source stored one via Agent 2's POST /sources/{id}/credential. `purpose="write"`
    callers never pass `credential_ciphertext` -- the write credential has no such
    column to read it from. Every error message names the ref, or the failure mode,
    never a value -- these messages reach logs and API responses."""
    if credential_ciphertext:
        try:
            return decrypt_credential(credential_ciphertext)
        except RopaConnectorError as exc:
            raise SourceNotAuthorizedError(
                f"{purpose} credential for this source could not be decrypted: {exc}"
            ) from exc
    if not credential_ref:
        return None
    if len(credential_ref) > _MAX_CREDENTIAL_REF_LEN or not credential_ref.replace("_", "").isalnum():
        raise SourceNotAuthorizedError(f"invalid {purpose} credential_ref format: {credential_ref!r}")
    secret = os.getenv(credential_ref)
    if secret is None:
        raise SourceNotAuthorizedError(
            f"{purpose} credential_ref {credential_ref!r} is not set in this environment; "
            "configure the secret before running this operation"
        )
    return secret


def build_grant(authorization: Any, *, source_name: str) -> SourceGrant:
    return resolve(authorization, source_name=source_name)


def build_connector(
    *,
    data_source: Any,
    authorization: Any,
    for_execution: bool = False,
):
    """Return a DSR connector for one authorized source.

    `for_execution=False` (the default) builds a search-only connector with no write
    credential resolved. Passing True is the ONLY way a write credential enters the
    process, and it fails closed if the authorization does not permit execution.
    """
    grant = build_grant(authorization, source_name=data_source.name)

    if for_execution and not grant.allow_execution:
        raise SourceNotAuthorizedError(
            f"source {data_source.name!r} is not authorized for DSR execution; "
            "refusing to build a write-capable connector"
        )

    connector = data_source.connector
    config = data_source.config or {}

    if connector == "postgres":
        missing = [k for k in ("host", "dbname", "user") if not config.get(k)]
        if missing:
            raise SourceNotAuthorizedError(f"postgres source config missing: {missing}")
        read_password = _resolve_secret(
            data_source.credential_ref,
            purpose="read",
            credential_ciphertext=getattr(data_source, "credential_ciphertext", None),
        )
        if read_password is None:
            raise SourceNotAuthorizedError(
                f"source {data_source.name!r} has no credential_ref or stored "
                "credential for its read password"
            )

        write_user = write_password = None
        if for_execution:
            # The write role may be a different user, or the same user with elevated
            # rights. `write_user` falls back to the read user only when the
            # configuration explicitly says so.
            write_password = _resolve_secret(grant.write_credential_ref, purpose="write")
            write_user = config.get("write_user") or config["user"]

        return PostgresDsrConnector(
            config=PostgresDsrConfig(
                host=config["host"],
                port=int(config.get("port", 5432)),
                dbname=config["dbname"],
                read_user=config["user"],
                read_password=read_password,
                write_user=write_user,
                write_password=write_password,
                # Optional: a source whose DSR tables are not in `public`.
                schema=config.get("schema") or config.get("search_path"),
                sslmode=config.get("sslmode", "require"),
                connect_timeout_seconds=float(config.get("connect_timeout_seconds", 5.0)),
                statement_timeout_seconds=float(config.get("statement_timeout_seconds", 15.0)),
            ),
            grant=grant,
        )

    raise SourceNotAuthorizedError(
        f"connector {connector!r} has no DSR implementation; "
        "a source must have a DSR connector before it can serve a data subject request"
    )
