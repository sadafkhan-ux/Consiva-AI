"""Reads the SHAPE of structured data. Never its contents.

WHAT IT READS, AND WHAT IT DELIBERATELY DOES NOT

Table names and column names, from `information_schema`. No row data, no samples, no
counts. The purpose of a table is indicated by what it is called and what fields it
holds; reading the values would mean this agent holding personal data in order to
decide whether personal data is being held properly, which is a trade nobody should
make for a marginal gain in classification accuracy.

SECURITY POSTURE

The connection string is supplied per run and never stored by this module. Credentials
are not logged, not echoed in errors, and not returned in any response -- `_safe()`
below strips them from anything that escapes.

The connection is opened read-only where the driver supports it, and every statement is
a parameterised read against `information_schema`. This module issues no DDL and no DML.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import asyncpg

# Schemas that describe the database itself rather than the organisation's data.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# A ceiling on how much shape one run will read. A schema with tens of thousands of
# columns is a real thing, and an unbounded read turns one assessment into an
# out-of-memory incident on the worker.
_MAX_TABLES = 500
_MAX_COLUMNS = 10_000

_CREDENTIAL = re.compile(r"//[^@/]+@")


def _safe(text: str) -> str:
    """Strip credentials from anything that might be logged or returned."""
    return _CREDENTIAL.sub("//***@", str(text))


class ConnectorError(RuntimeError):
    """A connection or read failed. Its message is always credential-safe."""


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        """`schema.table`, except for `public`, where the bare name is what people --
        and the declared-purpose records -- actually use."""
        return self.name if self.schema == "public" else f"{self.schema}.{self.name}"


@dataclass
class StructuredSchema:
    source_name: str
    tables: list[Table] = field(default_factory=list)
    truncated: bool = False

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)


async def read_postgres(dsn: str, *, source_name: str) -> StructuredSchema:
    """Read table and column names from a PostgreSQL database.

    Raises ConnectorError with a credential-safe message on any failure -- asyncpg's
    own errors routinely echo the DSN, and an error string ends up in a run record that
    a user can read.
    """
    if not dsn or not dsn.strip():
        raise ConnectorError("No connection string was supplied.")

    try:
        conn = await asyncpg.connect(dsn, timeout=15)
    except Exception as exc:
        raise ConnectorError(f"Could not connect: {_safe(exc)}") from None

    try:
        # One query for everything. Iterating tables and querying columns per table is
        # N+1 against a database that is not ours and may be across a network.
        rows = await conn.fetch(
            """
            select c.table_schema, c.table_name, c.column_name, c.data_type,
                   c.is_nullable
            from information_schema.columns c
            join information_schema.tables t
              on t.table_schema = c.table_schema and t.table_name = c.table_name
            where c.table_schema <> all($1::text[])
              and t.table_type = 'BASE TABLE'
            order by c.table_schema, c.table_name, c.ordinal_position
            """,
            list(_SYSTEM_SCHEMAS),
        )
    except Exception as exc:
        raise ConnectorError(f"Could not read the schema: {_safe(exc)}") from None
    finally:
        await conn.close()

    return _assemble(source_name, rows)


def _assemble(source_name: str, rows) -> StructuredSchema:
    schema = StructuredSchema(source_name=source_name)
    by_table: dict[tuple[str, str], Table] = {}
    columns_seen = 0

    for row in rows:
        key = (row["table_schema"], row["table_name"])
        if key not in by_table:
            if len(by_table) >= _MAX_TABLES:
                schema.truncated = True
                break
            by_table[key] = Table(schema=key[0], name=key[1])
        if columns_seen >= _MAX_COLUMNS:
            schema.truncated = True
            break
        by_table[key].columns.append(Column(
            name=row["column_name"],
            data_type=row["data_type"],
            nullable=(row["is_nullable"] == "YES"),
        ))
        columns_seen += 1

    schema.tables = list(by_table.values())
    return schema


def read_csv(content: str, *, source_name: str, table_name: str | None = None) -> StructuredSchema:
    """Read column names from a CSV header.

    Only the header is parsed. The body is not read at all -- a CSV export of a
    customer table is exactly the kind of file this agent must not ingest, and reading
    "just a few rows to improve classification" is how that starts.

    `table_name` names the logical table the file represents; without one the file's
    own name is used, since that is what a person would call it.
    """
    if not content.strip():
        raise ConnectorError("The uploaded file is empty.")

    try:
        reader = csv.reader(io.StringIO(content))
        header = next(reader, None)
    except csv.Error as exc:
        raise ConnectorError(f"Could not parse the CSV header: {exc}") from None

    if not header:
        raise ConnectorError("The uploaded file has no header row.")

    name = (table_name or source_name).strip()
    # A filename is a common and useful table name; the extension is not part of it.
    for suffix in (".csv", ".tsv", ".txt"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]

    columns = [
        Column(name=(col or "").strip(), data_type="text")
        for col in header
        if (col or "").strip()
    ]
    if not columns:
        raise ConnectorError("The header row contained no usable column names.")

    return StructuredSchema(
        source_name=source_name,
        tables=[Table(schema="public", name=name or "uploaded_file", columns=columns)],
    )
