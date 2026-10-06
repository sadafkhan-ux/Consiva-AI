"""Database-agnostic assembly of ROPA discovery evidence (ROPA prompt §2).

Shared by every relational connector (postgres.py, and any future engine such as
mysql.py). This module never opens a connection and never issues SQL -- it only
turns rows a connector has ALREADY fetched into TableRecord/ColumnRecord/
RelationshipRecord with local_ids assigned, and masks any sampled value to a
shape-only pattern. Keeping it blind to the driver is what lets two engines
share it without either one leaking into the other: a change to how samples
are masked, or to how local_ids are assigned, happens once, for every
connector at once, instead of drifting between copies.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agents.ropa.schemas.evidence import ColumnRecord, RelationshipRecord, TableRecord

# A ceiling on how much shape one run will read. A schema with tens of
# thousands of columns is a real thing, and an unbounded read turns one
# discovery into an out-of-memory incident on the worker -- the same reasoning
# purpose/connectors/structured.py already applies to its own schema reads,
# with the same numbers.
MAX_TABLES = 500
MAX_COLUMNS = 10_000


def mask_sample(value: object) -> str | None:
    """Reduce one sampled value to a shape-only pattern: digits become '#',
    letters become 'x', everything else (punctuation, whitespace, symbols) is
    kept as-is. This is the ONLY form a sample may ever take -- the caller
    discards the raw value the instant this returns."""
    if value is None:
        return None
    text = str(value)[:64]
    text = re.sub(r"[0-9]", "#", text)
    return re.sub(r"[A-Za-z]", "x", text)


@dataclass(frozen=True)
class RawTable:
    """One table/view row, as any engine's catalog reports it."""

    schema: str
    table: str


@dataclass(frozen=True)
class RawColumn:
    """One column row. Carries its own (schema, table) rather than a foreign
    key into RawTable, so a connector can build its column list independently
    of its table list -- exactly how both engines' catalogs actually read."""

    schema: str
    table: str
    column: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class RawRelationship:
    """One foreign-key edge between two discovered tables."""

    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str
    constraint_name: str | None = None


@dataclass(frozen=True)
class AssembledMetadata:
    tables: list[TableRecord]
    columns: list[ColumnRecord]
    relationships: list[RelationshipRecord]
    # True once either ceiling above was hit. The caller decides what to do
    # with this (log it, surface it) -- this module only reports it, since it
    # has no logger and no opinion on how loudly a truncation should be said.
    truncated: bool


# Awaited once per column, only when a connector supports sampling and the
# config asked for it. This is the ONLY place an engine's own SQL re-enters
# this function -- as a callback, never as a query string -- so this module
# itself never builds or runs a statement.
SamplePatternFetcher = Callable[[RawColumn], Awaitable[str | None]]


async def assemble(
    *,
    source_local_id: str,
    tables: list[RawTable],
    columns: list[RawColumn],
    relationships: list[RawRelationship],
    sample_pattern_for: SamplePatternFetcher | None = None,
) -> AssembledMetadata:
    """Turn one engine's catalog rows into ROPA evidence records.

    Column and relationship rows referencing a table that was dropped (outside
    the discovered scope, or past MAX_TABLES) are skipped rather than kept
    dangling -- a RelationshipRecord pointing at a table_local_id with no
    matching TableRecord would be evidence the rest of the pipeline cannot
    resolve.
    """
    truncated = False

    table_local_ids: dict[tuple[str, str], str] = {}
    table_records: list[TableRecord] = []
    for i, raw in enumerate(tables, start=1):
        if len(table_records) >= MAX_TABLES:
            truncated = True
            break
        local_id = f"table-{i}"
        table_local_ids[(raw.schema, raw.table)] = local_id
        table_records.append(
            TableRecord(
                local_id=local_id,
                source_local_id=source_local_id,
                schema_name=raw.schema,
                table_name=raw.table,
            )
        )

    column_records: list[ColumnRecord] = []
    for i, raw in enumerate(columns, start=1):
        if len(column_records) >= MAX_COLUMNS:
            truncated = True
            break
        table_local_id = table_local_ids.get((raw.schema, raw.table))
        if table_local_id is None:
            continue  # belongs to a table outside the discovered scope (e.g. a view)

        sample_pattern = await sample_pattern_for(raw) if sample_pattern_for else None
        column_records.append(
            ColumnRecord(
                local_id=f"column-{i}",
                table_local_id=table_local_id,
                column_name=raw.column,
                data_type=raw.data_type,
                nullable=raw.nullable,
                sample_pattern=sample_pattern,
            )
        )

    relationship_records: list[RelationshipRecord] = []
    for i, raw in enumerate(relationships, start=1):
        from_id = table_local_ids.get((raw.from_schema, raw.from_table))
        to_id = table_local_ids.get((raw.to_schema, raw.to_table))
        if from_id is None or to_id is None:
            continue  # references a table outside the discovered scope
        relationship_records.append(
            RelationshipRecord(
                local_id=f"rel-{i}",
                from_table_local_id=from_id,
                from_column=raw.from_column,
                to_table_local_id=to_id,
                to_column=raw.to_column,
                constraint_name=raw.constraint_name,
            )
        )

    return AssembledMetadata(
        tables=table_records,
        columns=column_records,
        relationships=relationship_records,
        truncated=truncated,
    )
