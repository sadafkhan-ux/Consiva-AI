"""What a DSR source is allowed to be asked to do (prompt §15, §16, §37).

This is the allowlist enforcement point. Every table name, every column name and
every operation passes through here before a connector builds SQL, and everything
fails closed: an unconfigured source permits nothing, an unlisted table permits
nothing, and a source without `allow_execution` permits no write no matter what
credential happens to exist in the environment.

Kept separate from the connector itself so the rules can be tested without a
database, and so a second connector (a CRM, an internal API) enforces the same
policy rather than reimplementing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.dsr.connectors.base import validate_identifier
from app.agents.dsr.errors import SourceNotAuthorizedError
from app.agents.dsr.schemas import case

# Identifier kinds a search may use. A kind outside this set cannot be configured
# into `identifier_columns` -- the vocabulary is closed so a typo becomes an error
# rather than a column nobody searches.
IDENTIFIER_KINDS = frozenset({"email", "phone", "reference"})


@dataclass(frozen=True)
class SourceGrant:
    """The resolved, validated authorization for one source.

    Built by `resolve()` from a `dsr_source_authorizations` row. Every name in it has
    already been validated as a SQL identifier, so a connector holding a SourceGrant
    can quote names without re-checking them.
    """

    source_name: str
    searchable_tables: tuple[str, ...]
    # Tables where one identifier value means one person. Two rows here for one email
    # is two candidate subjects; two rows in a non-identity table (an orders table) is
    # one person with two records. Without this distinction, "multiple matches" is
    # either a guess or it fires on every ordinary access request.
    identity_tables: frozenset[str]
    identifier_columns: dict[str, dict[str, str]]   # {table: {kind: column}}
    returnable_columns: dict[str, tuple[str, ...]]  # {table: (column, ...)}
    erasable_columns: dict[str, tuple[str, ...]]    # {table: (column, ...)}
    allow_execution: bool
    write_credential_ref: str | None

    # ── Search-side checks ───────────────────────────────────────────────────────

    def assert_searchable(self, table: str) -> None:
        if table not in self.searchable_tables:
            raise SourceNotAuthorizedError(
                f"table {table!r} is not in the DSR search allowlist for source "
                f"{self.source_name!r}"
            )

    def identifier_column(self, table: str, kind: str) -> str | None:
        """The column to match `kind` on in `table`, or None if this table has no
        column for that identifier. None is a normal answer, not an error -- an
        orders table may be searchable by email but have no phone column."""
        self.assert_searchable(table)
        return self.identifier_columns.get(table, {}).get(kind)

    def columns_for_disclosure(self, table: str) -> tuple[str, ...]:
        """The projection for an ACCESS response. Empty means nothing may be
        returned from this table, which is a configuration answer, not a bug --
        the connector then records the match without a snapshot."""
        self.assert_searchable(table)
        return self.returnable_columns.get(table, ())

    # ── Write-side checks ────────────────────────────────────────────────────────

    def assert_executable(self, table: str, operation: str) -> None:
        """The gate between "we found the record" and "we may change it".

        Checked immediately before execution, not once at plan time -- an
        authorization can be revoked between approval and execution, and §24
        requires revalidation at the moment of the write.
        """
        if operation not in case.OPERATIONS:
            raise SourceNotAuthorizedError(f"{operation!r} is not a DSR operation")
        if operation not in case.MUTATING_OPERATIONS:
            return  # disclose / retain / no_op touch nothing
        if not self.allow_execution:
            raise SourceNotAuthorizedError(
                f"source {self.source_name!r} is authorized for DSR search but not for "
                f"execution; {operation!r} is refused"
            )
        if not self.write_credential_ref:
            raise SourceNotAuthorizedError(
                f"source {self.source_name!r} allows execution but names no write "
                "credential; refusing to write with a read credential"
            )
        self.assert_searchable(table)

    def assert_erasable(self, table: str, column: str) -> None:
        """A deletion plan may only null or anonymize a column the administrator
        named. A column outside the list becomes a blocked action with a reason --
        never a silently skipped one (§47)."""
        allowed = self.erasable_columns.get(table, ())
        if column not in allowed:
            raise SourceNotAuthorizedError(
                f"column {table}.{column} is not in the erasable allowlist for source "
                f"{self.source_name!r}; this action must be reviewed rather than executed"
            )

    def assert_writable_columns(self, table: str, payload: dict[str, Any]) -> None:
        """Every column a write touches must be erasable-listed. A correction that
        tries to set a column nobody authorized is refused whole, not partially
        applied."""
        if not payload:
            raise SourceNotAuthorizedError(
                f"a write against {table!r} named no columns; refusing an empty update"
            )
        for column in payload:
            validate_identifier(column, kind="column")
            self.assert_erasable(table, column)


def resolve(authorization: Any, *, source_name: str) -> SourceGrant:
    """Turn a `dsr_source_authorizations` row into a validated SourceGrant.

    Every identifier is validated HERE, once, rather than at each use. A malformed
    name in the configuration fails loudly at resolve time instead of reaching a
    query builder.
    """
    if authorization is None:
        raise SourceNotAuthorizedError(
            f"source {source_name!r} has no DSR authorization; registering a source for "
            "Agent 2 discovery does not authorize it for DSR"
        )
    if not getattr(authorization, "enabled", False):
        raise SourceNotAuthorizedError(f"DSR authorization for {source_name!r} is disabled")

    tables = tuple(
        validate_identifier(t, kind="table") for t in (authorization.searchable_tables or [])
    )
    table_set = set(tables)

    identity_tables = frozenset(
        validate_identifier(t, kind="table")
        for t in (getattr(authorization, "identity_tables", None) or [])
    )
    if stray := identity_tables - table_set:
        raise SourceNotAuthorizedError(
            f"identity_tables names {sorted(stray)}, which are not in searchable_tables "
            f"for {source_name!r}"
        )

    identifier_columns: dict[str, dict[str, str]] = {}
    for table, mapping in (authorization.identifier_columns or {}).items():
        if table not in table_set:
            raise SourceNotAuthorizedError(
                f"identifier_columns names table {table!r}, which is not in "
                f"searchable_tables for {source_name!r}"
            )
        resolved: dict[str, str] = {}
        for kind, column in (mapping or {}).items():
            if kind not in IDENTIFIER_KINDS:
                raise SourceNotAuthorizedError(
                    f"{kind!r} is not a DSR identifier kind; expected one of "
                    f"{sorted(IDENTIFIER_KINDS)}"
                )
            resolved[kind] = validate_identifier(column, kind="column")
        identifier_columns[table] = resolved

    returnable = {
        table: tuple(validate_identifier(c, kind="column") for c in (cols or []))
        for table, cols in (authorization.returnable_columns or {}).items()
        if table in table_set
    }
    erasable = {
        table: tuple(validate_identifier(c, kind="column") for c in (cols or []))
        for table, cols in (authorization.erasable_columns or {}).items()
        if table in table_set
    }

    return SourceGrant(
        source_name=source_name,
        searchable_tables=tables,
        identity_tables=identity_tables,
        identifier_columns=identifier_columns,
        returnable_columns=returnable,
        erasable_columns=erasable,
        allow_execution=bool(authorization.allow_execution),
        write_credential_ref=authorization.write_credential_ref,
    )
