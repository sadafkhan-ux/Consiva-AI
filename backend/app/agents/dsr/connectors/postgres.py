"""PostgreSQL connector for Agent 3 (prompt §16).

Talks to the ORG'S authorized source database -- never Consiva's own (see
app/db/session.py for that) -- over a short-lived asyncpg connection that is always
closed before returning.

HOW SQL IS BUILT, AND WHY IT IS SAFE
------------------------------------
There are exactly two kinds of thing in every statement here:

  * VALUES -- the requester's email, a corrected phone number, a primary key. These
    are always bound parameters ($1, $2 ...). They are never formatted into the
    statement, and asyncpg never does client-side interpolation, so a value cannot
    become syntax.

  * IDENTIFIERS -- table and column names. SQL cannot parameterize these, so they
    are constrained twice: they must be present in the administrator's allowlist
    (`SourceGrant`, which already validated their shape), and they are quoted with
    `quote_identifier` at the point of use, which validates again.

No caller supplies an identifier. Not the requester, not the API, not the LLM. They
come only from `dsr_source_authorizations`, which only an administrator writes. That
is what "the LLM must never generate arbitrary SQL" means in practice here: there is
no code path from generated text to an identifier or to a statement.

READ AND WRITE ARE DIFFERENT CREDENTIALS
----------------------------------------
`search` uses the read credential. `execute_action` uses the write credential named
by the authorization, and refuses to run at all if the authorization does not name
one -- a write is never attempted on a read connection, and a source authorized only
for search cannot be written to even by a caller holding a write password.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.agents.dsr.connectors import base
from app.agents.dsr.connectors.authorization import SourceGrant
from app.agents.dsr.connectors.base import (
    ExecutionOutcome,
    SearchOutcome,
    SubjectMatch,
    quote_identifier,
)
from app.agents.dsr.errors import (
    ActionFailedError,
    ConnectorTimeoutError,
    ConnectorUnavailableError,
    SearchFailedError,
    SourceNotAuthorizedError,
    VerificationFailedError,
)
from app.agents.dsr.schemas import case

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostgresDsrConfig:
    """Non-secret connection details plus the two credentials.

    Both passwords arrive already resolved from the secret store. They are never
    logged, never returned, and `__repr__` is suppressed on the dataclass so an
    accidental log of the config object cannot leak them.
    """

    host: str
    port: int
    dbname: str
    read_user: str
    read_password: str
    write_user: str | None = None
    write_password: str | None = None
    # The schema the allowlisted tables live in. None means "resolve through the
    # connection's search_path", which is `public` in practice -- fine for a source
    # that keeps its tables there, and wrong for every source that does not.
    schema: str | None = None
    sslmode: str = "require"
    connect_timeout_seconds: float = base.DEFAULT_CONNECT_TIMEOUT_SECONDS
    statement_timeout_seconds: float = base.DEFAULT_STATEMENT_TIMEOUT_SECONDS

    def __repr__(self) -> str:  # pragma: no cover -- trivial, but load-bearing
        return (
            f"PostgresDsrConfig(host={self.host!r}, port={self.port}, "
            f"dbname={self.dbname!r}, read_user={self.read_user!r}, "
            "read_password=<redacted>, write_password=<redacted>)"
        )


class PostgresDsrConnector:
    """DSR connector for one authorized PostgreSQL source."""

    connector_name = "postgres"

    def __init__(self, *, config: PostgresDsrConfig, grant: SourceGrant):
        self._config = config
        self._grant = grant

    def _qualified(self, table: str) -> str:
        """`"schema"."table"`, or just `"table"` when no schema is configured.

        The schema goes through the same validate-then-quote path as every other
        identifier: it comes from source configuration, not from a caller, but it is
        still interpolated into a statement, so it is held to the same rule.
        """
        quoted = quote_identifier(table, kind="table")
        if not self._config.schema:
            return quoted
        return f"{quote_identifier(self._config.schema, kind='schema')}.{quoted}"

    # ── Connections ──────────────────────────────────────────────────────────────

    async def _connect(self, *, write: bool) -> asyncpg.Connection:
        """Open a connection with the credential appropriate to the operation.

        A write connection is only ever opened for an operation the grant has
        already authorized; `assert_executable` runs before this is called.
        """
        if write:
            if not (self._config.write_user and self._config.write_password):
                raise SourceNotAuthorizedError(
                    f"source {self._grant.source_name!r} has no resolved write credential; "
                    "refusing to attempt a write on the read connection"
                )
            user, password = self._config.write_user, self._config.write_password
        else:
            user, password = self._config.read_user, self._config.read_password

        try:
            conn = await asyncpg.connect(
                host=self._config.host, port=self._config.port, database=self._config.dbname,
                user=user, password=password, ssl=self._config.sslmode,
                timeout=self._config.connect_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ConnectorTimeoutError(
                f"connecting to source {self._grant.source_name!r} timed out"
            ) from exc
        except (OSError, asyncpg.PostgresError) as exc:
            # The message deliberately does not echo the driver's text: it can carry
            # the host, the user, and occasionally the credential.
            logger.warning(
                "DSR connector could not reach source %s (%s)",
                self._grant.source_name, type(exc).__name__,
            )
            raise ConnectorUnavailableError(
                f"source {self._grant.source_name!r} is unreachable or rejected the connection"
            ) from exc

        await conn.execute(
            f"SET statement_timeout = {int(self._config.statement_timeout_seconds * 1000)}"
        )
        return conn

    async def test_connection(self) -> bool:
        conn = await self._connect(write=False)
        try:
            return await conn.fetchval("SELECT 1") == 1
        finally:
            await conn.close()

    # ── Search (§17) ─────────────────────────────────────────────────────────────

    async def search_subject(
        self, *, identifiers: dict[str, str], limit: int = base.MAX_ROWS_PER_TABLE
    ) -> SearchOutcome:
        """Search every allowlisted table for the requester's identifiers.

        Deterministic matching only: exact, and case-insensitive exact for email
        (which is routinely stored with different capitalisation than it is typed).
        No fuzzy matching, no LIKE, no trigram similarity -- a DSR match must be one
        a human can check, and "probably this person" is not that.
        """
        usable = {k: v for k, v in (identifiers or {}).items() if v and v.strip()}
        if not usable:
            raise SearchFailedError("no usable identifier was supplied for the search")

        capped = min(limit, base.MAX_ROWS_PER_TABLE)
        matches: list[SubjectMatch] = []
        searched: list[str] = []
        notes: list[str] = []
        truncated = False

        conn = await self._connect(write=False)
        try:
            for table in self._grant.searchable_tables:
                columns_by_kind = {
                    kind: column
                    for kind in usable
                    if (column := self._grant.identifier_column(table, kind))
                }
                if not columns_by_kind:
                    notes.append(f"{table}: no configured column for the supplied identifiers")
                    continue
                searched.append(table)
                for kind, column in columns_by_kind.items():
                    rows, hit_cap = await self._match_rows(
                        conn, table=table, column=column, value=usable[kind], limit=capped,
                    )
                    truncated = truncated or hit_cap
                    if hit_cap:
                        notes.append(
                            f"{table}.{column}: more than {capped} rows matched; "
                            "the identifier is too broad to fulfil automatically"
                        )
                    for row in rows:
                        matches.append(
                            self._to_match(table=table, column=column, kind=kind, row=row)
                        )
        finally:
            await conn.close()

        return SearchOutcome(
            matches=tuple(matches),
            tables_searched=tuple(searched),
            distinct_subjects=self._count_distinct_subjects(matches),
            truncated=truncated,
            notes=tuple(notes),
        )

    async def _match_rows(
        self, conn: asyncpg.Connection, *, table: str, column: str, value: str, limit: int
    ) -> tuple[list[asyncpg.Record], bool]:
        """One table, one identifier column. Fetches limit+1 rows so "there were more"
        is distinguishable from "there were exactly limit"."""
        quoted_table = self._qualified(table)
        quoted_column = quote_identifier(column, kind="column")
        projection = self._projection(table, column)

        # lower(...) on both sides for email-shaped identifiers. The VALUE is still a
        # bound parameter; only the already-allowlisted identifiers are interpolated.
        predicate = (
            f"lower({quoted_column}::text) = lower($1)"
            if "@" in value
            else f"{quoted_column}::text = $1"
        )
        sql = f"SELECT {projection} FROM {quoted_table} WHERE {predicate} LIMIT {limit + 1}"

        try:
            rows = await conn.fetch(sql, value)
        except asyncpg.QueryCanceledError as exc:
            raise ConnectorTimeoutError(
                f"search of {table}.{column} exceeded the statement timeout"
            ) from exc
        except asyncpg.PostgresError as exc:
            logger.warning("DSR search failed on %s.%s (%s)", table, column, type(exc).__name__)
            raise SearchFailedError(
                f"searching {table}.{column} failed; the source rejected the query"
            ) from exc

        if len(rows) > limit:
            return rows[:limit], True
        return list(rows), False

    def _projection(self, table: str, matched_column: str) -> str:
        """The column list for a search. Always explicit, never `SELECT *` (§18).

        Includes the primary-key-ish locator columns plus whatever the administrator
        marked returnable. When nothing is returnable, the match is still recorded --
        it just carries no snapshot.
        """
        wanted = {
            *self._grant.key_columns(table),
            matched_column,
            *self._grant.columns_for_disclosure(table),
        }
        return ", ".join(quote_identifier(c, kind="column") for c in sorted(wanted))

    def _to_match(
        self, *, table: str, column: str, kind: str, row: asyncpg.Record
    ) -> SubjectMatch:
        data = dict(row)
        keys = self._grant.key_columns(table)
        missing = [k for k in keys if k not in data]
        if missing:
            # The configured locator is not in the row. Never fall back to matching on
            # the identifier column: that is not unique, so execution would later
            # address the wrong record -- or several.
            raise SearchFailedError(
                f"record_key_columns for {table} names {missing}, which the source did "
                "not return; correct the DSR source authorization before searching"
            )
        reference = {k: data[k] for k in keys}
        returnable = self._grant.columns_for_disclosure(table)
        snapshot = {k: _jsonable(v) for k, v in data.items() if k in returnable} or None
        reference = {k: _jsonable(v) for k, v in reference.items()}
        return SubjectMatch(
            table_name=table,
            matched_column=column,
            identifier_kind=kind,
            # Case-insensitive email equality is still an exact match on the value,
            # just not on its capitalisation -- recorded distinctly so a reviewer can
            # see which rows needed normalizing.
            match_type="normalized_exact" if kind == "email" else "exact",
            confidence=1.0,
            record_reference=reference,
            record_snapshot=snapshot,
            schema_name=self._config.schema,
        )

    def _count_distinct_subjects(self, matches: list[SubjectMatch]) -> int:
        """How many different PEOPLE the matches describe.

        Only identity tables are counted. Two rows in `customers` for one email are
        two candidate subjects and the case must stop for a human; two rows in
        `orders` for the same email are one person with two orders. Inferring that
        distinction instead of configuring it would either guess or fire on every
        ordinary access request.

        A source with no identity table configured reports 1 whenever anything
        matched: the connector genuinely cannot tell, so it does not claim to. The
        search service treats that as a review signal rather than a clean result.
        """
        identity_records = {
            (m.table_name, tuple(sorted(m.record_reference.items())))
            for m in matches
            if m.table_name in self._grant.identity_tables
        }
        if identity_records:
            return len(identity_records)
        return 1 if matches else 0

    @property
    def has_identity_table(self) -> bool:
        """False when the administrator configured no identity table for this source,
        which means `distinct_subjects` cannot be trusted to detect ambiguity."""
        return bool(self._grant.identity_tables)

    # ── Execution (§24) ──────────────────────────────────────────────────────────

    async def execute_action(
        self, *, table_name: str, record_reference: dict[str, Any], operation: str,
        payload: dict[str, Any],
    ) -> ExecutionOutcome:
        """Perform one approved operation against one record, then read it back.

        The read-back is not optional and its result is not merged into the write's
        own claim: `rows_affected` says what the database reported, `verified` says
        what a fresh SELECT found. A write that reports success but does not verify
        raises rather than returning a success (§24).
        """
        self._grant.assert_executable(table_name, operation)
        if operation not in case.MUTATING_OPERATIONS:
            return ExecutionOutcome(
                rows_affected=0, verified=True,
                verification_detail={"operation": operation, "note": "no source write required"},
            )
        if not record_reference:
            raise ActionFailedError("cannot execute against an empty record reference")

        conn = await self._connect(write=True)
        try:
            async with conn.transaction():
                rows_affected = await self._apply(
                    conn, table=table_name, reference=record_reference,
                    operation=operation, payload=payload,
                )
                verified, detail = await self._verify(
                    conn, table=table_name, reference=record_reference,
                    operation=operation, payload=payload,
                )
                if not verified:
                    # Roll back rather than leave a half-done, unverifiable change.
                    raise VerificationFailedError(
                        f"{operation} on {table_name} reported {rows_affected} row(s) affected "
                        f"but the read-back did not confirm it: {detail}"
                    )
        except (VerificationFailedError, SourceNotAuthorizedError):
            raise
        except asyncpg.QueryCanceledError as exc:
            raise ConnectorTimeoutError(
                f"{operation} on {table_name} exceeded the statement timeout"
            ) from exc
        except asyncpg.PostgresError as exc:
            logger.warning(
                "DSR execution failed on %s (%s)", table_name, type(exc).__name__
            )
            raise ActionFailedError(
                f"{operation} on {table_name} was rejected by the source"
            ) from exc
        finally:
            await conn.close()

        return ExecutionOutcome(
            rows_affected=rows_affected, verified=True, verification_detail=detail,
            raw_response={"operation": operation, "table": table_name},
        )

    async def _apply(
        self, conn: asyncpg.Connection, *, table: str, reference: dict[str, Any],
        operation: str, payload: dict[str, Any],
    ) -> int:
        quoted_table = self._qualified(table)
        where_sql, where_args = self._where(reference, start=1)

        if operation == case.OP_DELETE_RECORD:
            status = await conn.execute(f"DELETE FROM {quoted_table} WHERE {where_sql}", *where_args)
            return _affected(status)

        # update_field and anonymize_field are the same statement shape; they differ
        # only in what the payload holds (a new value vs an explicit null).
        self._grant.assert_writable_columns(table, payload)
        columns = sorted(payload)
        assignments = ", ".join(
            f"{quote_identifier(c, kind='column')} = ${i}" for i, c in enumerate(columns, start=1)
        )
        set_args = [payload[c] for c in columns]
        where_sql, where_args = self._where(reference, start=len(set_args) + 1)
        status = await conn.execute(
            f"UPDATE {quoted_table} SET {assignments} WHERE {where_sql}", *set_args, *where_args,
        )
        return _affected(status)

    async def _verify(
        self, conn: asyncpg.Connection, *, table: str, reference: dict[str, Any],
        operation: str, payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Read the record back and check the intended end state actually holds."""
        quoted_table = self._qualified(table)
        where_sql, where_args = self._where(reference, start=1)

        if operation == case.OP_DELETE_RECORD:
            remaining = await conn.fetchval(
                f"SELECT count(*) FROM {quoted_table} WHERE {where_sql}", *where_args
            )
            return remaining == 0, {"check": "record absent", "rows_remaining": int(remaining)}

        columns = sorted(payload)
        projection = ", ".join(quote_identifier(c, kind="column") for c in columns)
        row = await conn.fetchrow(
            f"SELECT {projection} FROM {quoted_table} WHERE {where_sql}", *where_args
        )
        if row is None:
            return False, {"check": "record present after update", "found": False}
        actual = dict(row)
        mismatches = {
            c: {"expected": _jsonable(payload[c]), "actual": _jsonable(actual.get(c))}
            for c in columns
            if actual.get(c) != payload[c]
        }
        return not mismatches, {"check": "values match", "mismatches": mismatches}

    def _where(self, reference: dict[str, Any], *, start: int) -> tuple[str, list[Any]]:
        """Build the record locator. Keys are validated identifiers from the search's
        own record_reference; values are bound parameters."""
        clauses, args = [], []
        for offset, (column, value) in enumerate(sorted(reference.items())):
            clauses.append(f"{quote_identifier(column, kind='column')} = ${start + offset}")
            args.append(value)
        if not clauses:
            raise ActionFailedError("refusing to build a WHERE clause with no conditions")
        return " AND ".join(clauses), args


def _affected(status: str) -> int:
    """asyncpg returns a command tag like 'UPDATE 1' / 'DELETE 3'."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


def _jsonable(value: Any) -> Any:
    """JSONB columns hold what we give them, and a UUID or a datetime is not JSON.
    Everything stored as evidence goes through here."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


base.register(PostgresDsrConnector.connector_name, PostgresDsrConnector)
