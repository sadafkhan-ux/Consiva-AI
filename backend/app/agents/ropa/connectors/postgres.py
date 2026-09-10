"""PostgreSQL discovery connector for the ROPA agent.

Two-step flow, matching the agent's build plan:

    1. Connect Source  -- connect with the org's authorized credentials, then
       verify the role is least-privilege (not a superuser / bypass-RLS role)
       before any read happens.
    2. Discover         -- read only the schema/table/column metadata that
       role is actually permitted to see, plus an optional redacted sample.

This talks to the ORG'S target database being discovered -- never the app's
own database (see app/db/session.py for that) -- over a short-lived asyncpg
connection that is always closed before returning.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import asyncpg

from app.agents.ropa.connectors.base import register
from app.agents.ropa.schemas.evidence import (
    ColumnRecord,
    DiscoveryEvidence,
    RelationshipRecord,
    SourceRecord,
    TableRecord,
)

# information_schema/pg_catalog schemas are never customer data.
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


class PostgresConnectorError(Exception):
    """Raised for anything that stops discovery before evidence can be
    returned: bad credentials, an over-privileged role, unreachable host, or
    a requested schema that doesn't exist."""


@dataclass(frozen=True)
class PostgresConnectionConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str = "require"
    # Restrict discovery to specific schemas. None means "every non-system schema
    # this role can see" -- Postgres already filters information_schema by
    # privilege, so an under-privileged role naturally sees less, never more.
    schemas: tuple[str, ...] | None = None
    # Off by default -- evidence must stay to metadata unless a sample is
    # explicitly authorized, and even then only as a redacted pattern (prompt §2, §3).
    collect_sample_patterns: bool = False
    connect_timeout_seconds: float = 5.0
    # Server-side cap on any single discovery query.
    statement_timeout_seconds: float = 30.0
    # Safety valve for a break-glass admin credential during setup/testing --
    # never enable this for a real discovery run against customer data.
    allow_superuser: bool = False


# ---------------------------------------------------------------------------
# Step 1: Connect Source
# ---------------------------------------------------------------------------


async def _connect(config: PostgresConnectionConfig) -> asyncpg.Connection:
    """Connect and validate credentials -- asyncpg fails fast on bad auth or
    an unreachable host, so a successful connect() *is* the credential-
    validation half of this step."""
    try:
        return await asyncpg.connect(
            host=config.host,
            port=config.port,
            database=config.dbname,
            user=config.user,
            password=config.password,
            ssl=None if config.sslmode == "disable" else config.sslmode,
            timeout=config.connect_timeout_seconds,
        )
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError) as exc:
        raise PostgresConnectorError(f"credential validation failed: {exc}") from exc
    except (OSError, asyncpg.PostgresError) as exc:
        raise PostgresConnectorError(f"could not connect to source: {exc}") from exc


async def _verify_least_privilege(conn: asyncpg.Connection, config: PostgresConnectionConfig) -> None:
    """Refuse to run discovery over a superuser / RLS-bypassing role. The
    agent must only ever hold read access scoped to what it needs -- if the
    supplied credential can do more than that, connecting with it at all is
    the actual privacy risk, independent of what discovery goes on to read."""
    row = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if row is None:
        raise PostgresConnectorError(f"could not resolve role privileges for {config.user!r}")
    if (row["rolsuper"] or row["rolbypassrls"]) and not config.allow_superuser:
        raise PostgresConnectorError(
            f"role {config.user!r} is a superuser / bypass-RLS role -- "
            "discovery requires a least-privilege, read-only credential"
        )


async def _enforce_read_only(conn: asyncpg.Connection, config: PostgresConnectionConfig) -> None:
    """Make writes impossible for the rest of this session, enforced by Postgres
    itself rather than by convention.

    With `default_transaction_read_only = on`, the SERVER rejects INSERT, UPDATE,
    DELETE, DROP, ALTER and TRUNCATE with "cannot execute ... in a read-only
    transaction" -- so a bug, a bad config, or a future edit to this module still
    cannot modify the customer's production database. `statement_timeout` bounds
    any single query so a pathological table can't hang discovery.
    """
    await conn.execute("SET default_transaction_read_only = on")
    await conn.execute(f"SET statement_timeout = {int(config.statement_timeout_seconds * 1000)}")


async def connect_source(config: PostgresConnectionConfig) -> asyncpg.Connection:
    """Connect to the org's authorized source with a verified least-privilege
    credential, then lock the session read-only. Returns an open connection; the
    caller is responsible for closing it (discover() below does this via
    try/finally)."""
    conn = await _connect(config)
    try:
        await _verify_least_privilege(conn, config)
        await _enforce_read_only(conn, config)
    except Exception:
        await conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Step 2: Discover
# ---------------------------------------------------------------------------


async def _read_schema_metadata(conn: asyncpg.Connection, requested: tuple[str, ...] | None) -> list[str]:
    """Which schemas discovery is allowed to walk -- information_schema only
    ever lists what the connected role is permitted to see."""
    rows = await conn.fetch(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name != ALL($1::text[])",
        list(_SYSTEM_SCHEMAS),
    )
    available = {row["schema_name"] for row in rows}
    if requested is None:
        return sorted(available)
    missing = set(requested) - available
    if missing:
        raise PostgresConnectorError(f"requested schema(s) not found or not permitted: {sorted(missing)}")
    return sorted(requested)


async def _read_tables(conn: asyncpg.Connection, schema_names: list[str]) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY($1::text[]) AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
        """,
        schema_names,
    )


async def _read_relationships(conn: asyncpg.Connection, schema_names: list[str]) -> list[asyncpg.Record]:
    """Foreign keys between discovered tables. This is the structural evidence
    processing_activity_service uses to corroborate that two tables belong to
    the same business activity.

    Reads pg_catalog, NOT information_schema.constraint_column_usage. That view
    only exposes constraints on tables the current role OWNS, so a genuine
    least-privilege read-only role -- exactly what this connector demands -- sees
    zero foreign keys through it. Confirmed live on a real database: superuser 34
    FKs / read-only role 0 via information_schema, both 34 via pg_catalog. The
    catalog is readable by any role with schema usage, so this works identically
    for both.
    """
    return await conn.fetch(
        """
        SELECT
            c.conname                    AS constraint_name,
            fn.nspname                   AS from_schema,
            ft.relname                   AS from_table,
            fa.attname                   AS from_column,
            tn.nspname                   AS to_schema,
            tt.relname                   AS to_table,
            ta.attname                   AS to_column
        FROM pg_constraint c
        JOIN pg_class     ft ON ft.oid = c.conrelid
        JOIN pg_namespace fn ON fn.oid = ft.relnamespace
        JOIN pg_class     tt ON tt.oid = c.confrelid
        JOIN pg_namespace tn ON tn.oid = tt.relnamespace
        -- Composite keys produce one row per column pair, matched by ordinal.
        JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(src, tgt, ord) ON TRUE
        JOIN pg_attribute fa ON fa.attrelid = c.conrelid  AND fa.attnum = k.src
        JOIN pg_attribute ta ON ta.attrelid = c.confrelid AND ta.attnum = k.tgt
        WHERE c.contype = 'f'
          AND fn.nspname = ANY($1::text[])
        ORDER BY c.conname, k.ord
        """,
        schema_names,
    )


async def _read_columns(conn: asyncpg.Connection, schema_names: list[str]) -> list[asyncpg.Record]:
    """Columns and data types come from the same information_schema view --
    one query covers both metadata reads."""
    return await conn.fetch(
        """
        SELECT table_schema, table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = ANY($1::text[])
        ORDER BY table_schema, table_name, ordinal_position
        """,
        schema_names,
    )


def _sample_pattern(value: object) -> str | None:
    """Reduce one sampled value to a shape-only pattern: digits become '#',
    letters become 'x', everything else (punctuation, whitespace, symbols) is
    kept as-is. This is the ONLY form a sample may ever take -- the caller
    discards the raw value the instant this returns."""
    if value is None:
        return None
    text = str(value)[:64]
    text = re.sub(r"[0-9]", "#", text)
    return re.sub(r"[A-Za-z]", "x", text)


def _quote_ident(identifier: str) -> str:
    """Postgres identifier quoting. These names come from information_schema (the
    database's own catalog, not user input), but a table legitimately named
    `foo"bar` would otherwise break out of the quoted string below."""
    return '"' + identifier.replace('"', '""') + '"'


async def _read_sample_pattern(conn: asyncpg.Connection, schema: str, table: str, column: str) -> str | None:
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    col = _quote_ident(column)
    try:
        value = await conn.fetchval(
            f"SELECT {col} FROM {target} WHERE {col} IS NOT NULL LIMIT 1"
        )
    except asyncpg.PostgresError:
        # An unreadable column/table (permissions, exotic type) is not a
        # discovery failure -- just skip the sample for it.
        return None
    return _sample_pattern(value)


async def discover_metadata(conn: asyncpg.Connection, config: PostgresConnectionConfig) -> tuple[
    list[TableRecord], list[ColumnRecord], list[RelationshipRecord]
]:
    """Read permitted schema, table, column, and data-type metadata over an
    already-connected, already-verified connection (step 2 of the flow)."""
    source_local_id = "source-1"
    schema_names = await _read_schema_metadata(conn, config.schemas)

    table_rows = await _read_tables(conn, schema_names)
    table_local_ids: dict[tuple[str, str], str] = {}
    tables: list[TableRecord] = []
    for i, row in enumerate(table_rows, start=1):
        local_id = f"table-{i}"
        table_local_ids[(row["table_schema"], row["table_name"])] = local_id
        tables.append(
            TableRecord(
                local_id=local_id,
                source_local_id=source_local_id,
                schema_name=row["table_schema"],
                table_name=row["table_name"],
            )
        )

    column_rows = await _read_columns(conn, schema_names)
    columns: list[ColumnRecord] = []
    for i, row in enumerate(column_rows, start=1):
        table_local_id = table_local_ids.get((row["table_schema"], row["table_name"]))
        if table_local_id is None:
            continue  # belongs to a view/foreign table outside table_type = 'BASE TABLE'

        sample_pattern = None
        if config.collect_sample_patterns:
            sample_pattern = await _read_sample_pattern(
                conn, row["table_schema"], row["table_name"], row["column_name"]
            )

        columns.append(
            ColumnRecord(
                local_id=f"column-{i}",
                table_local_id=table_local_id,
                column_name=row["column_name"],
                data_type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                sample_pattern=sample_pattern,
            )
        )

    relationship_rows = await _read_relationships(conn, schema_names)
    relationships: list[RelationshipRecord] = []
    for i, row in enumerate(relationship_rows, start=1):
        from_id = table_local_ids.get((row["from_schema"], row["from_table"]))
        to_id = table_local_ids.get((row["to_schema"], row["to_table"]))
        if from_id is None or to_id is None:
            continue  # references a table outside the discovered scope
        relationships.append(
            RelationshipRecord(
                local_id=f"rel-{i}",
                from_table_local_id=from_id,
                from_column=row["from_column"],
                to_table_local_id=to_id,
                to_column=row["to_column"],
                constraint_name=row["constraint_name"],
            )
        )

    return tables, columns, relationships


# ---------------------------------------------------------------------------
# Entry point: runs both steps and returns structured evidence
# ---------------------------------------------------------------------------


async def discover(*, org_id: str, source_name: str, config: PostgresConnectionConfig) -> DiscoveryEvidence:
    """Run both steps (connect_source -> discover_metadata) against one
    PostgreSQL source and return structured evidence. Classification, purpose
    inference, and everything downstream happen in the agent's services."""
    discovery_run_id = str(uuid.uuid4())
    source_local_id = "source-1"

    conn = await connect_source(config)
    try:
        tables, columns, relationships = await discover_metadata(conn, config)
    finally:
        await conn.close()

    return DiscoveryEvidence(
        org_id=org_id,
        discovery_run_id=discovery_run_id,
        sources=[
            SourceRecord(
                local_id=source_local_id,
                name=source_name,
                source_type="database",
                connector="postgres",
                location=config.host,
            )
        ],
        tables=tables,
        columns=columns,
        relationships=relationships,
    )


class PostgresConnector:
    """SourceConnector adapter over the functions above, so Postgres is reachable
    through the generic registry (connectors/base.py) rather than by import."""

    source_type = "database"
    connector_name = "postgres"

    def __init__(self, config: PostgresConnectionConfig) -> None:
        self._config = config

    async def discover(self, *, org_id: str, source_name: str) -> DiscoveryEvidence:
        return await discover(org_id=org_id, source_name=source_name, config=self._config)


register("postgres", PostgresConnector)
