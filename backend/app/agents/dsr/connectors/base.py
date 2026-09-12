"""The DSR connector contract.

WHY THIS IS NOT app/agents/ropa/connectors/base.py
--------------------------------------------------
Agent 2's `SourceConnector` states, as its contract, that implementations "must
never write to, modify, or delete anything in the source". Agent 3 must correct and
delete. Widening that Protocol to allow writes would hand write capability to the
discovery path as a side effect, and discovery runs against production databases
under a standing read-only constraint. So DSR gets its own protocol, and Agent 2's
stays exactly as it is.

The separation is not only a type: a DSR source needs its own authorization row
(`dsr_source_authorizations`) and, for execution, its own `write_credential_ref`
naming a different secret. A source registered for Agent 2 discovery is authorized
for nothing here until an administrator says otherwise, and search authorization
never implies execution authorization.

THE SQL RULE
------------
Values are ALWAYS parameterized. Identifiers (table and column names) cannot be
parameterized in SQL, so they are constrained twice instead: they must appear in the
administrator's allowlist, and they must satisfy `validate_identifier` before being
quoted. Text that came from a requester, or from a model, is never either of those
things -- it only ever arrives as a bound parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.agents.dsr.errors import SourceNotAuthorizedError

# A hard cap the administrator's configuration cannot raise. A DSR search that
# matches thousands of rows is not a DSR search -- it is a mis-scoped identifier, and
# returning the first 500 of them would be worse than stopping.
MAX_ROWS_PER_TABLE = 500
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 15.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

# Postgres identifiers we are willing to quote. Deliberately narrower than what
# Postgres itself permits: an allowlisted name containing a quote, a backslash or a
# null byte is a configuration mistake or an attack, and neither should be quoted
# and run. This runs on names that already passed the allowlist, as defence in depth.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
    """Return `name` unchanged, or raise. Never returns a modified name -- silently
    sanitizing an identifier would mean querying something other than what the
    configuration said."""
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise SourceNotAuthorizedError(
            f"{kind} {name!r} is not a valid unquoted SQL identifier; "
            "check the source authorization configuration."
        )
    return name


def quote_identifier(name: str, *, kind: str = "identifier") -> str:
    """Validate, then double-quote for Postgres. Both steps, always."""
    return '"' + validate_identifier(name, kind=kind) + '"'


@dataclass(frozen=True)
class SubjectMatch:
    """One matched record. Everything a `dsr_evidence` row needs (§17).

    `record_reference` is the primary-key locator for the row. Execution later
    addresses the record through THIS, never by re-running the search -- re-running
    could match a different row if the data changed in between.
    """

    table_name: str
    matched_column: str
    identifier_kind: str
    match_type: str
    confidence: float
    record_reference: dict[str, Any]
    record_snapshot: dict[str, Any] | None = None
    schema_name: str | None = None


@dataclass(frozen=True)
class SearchOutcome:
    """The result of searching ONE source.

    `distinct_subjects` is the count that decides whether the case may proceed
    automatically. Two distinct subjects means the identifier matched two different
    people, and handing one of them the other's data is the worst outcome a DSR
    system can produce -- so that stops for a human (§42 scenario 5).
    """

    matches: tuple[SubjectMatch, ...] = ()
    tables_searched: tuple[str, ...] = ()
    distinct_subjects: int = 0
    truncated: bool = False
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ExecutionOutcome:
    """What a write actually did, and what a fresh read-back found afterwards.

    `rows_affected` is what the database reported. `verified` is whether a separate
    read confirmed the intended end state. They are separate fields because they are
    separate claims: a command returning "1 row updated" is not evidence that the
    data now reads the way the DSR required (§24).
    """

    rows_affected: int
    verified: bool
    verification_detail: dict[str, Any]
    raw_response: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DsrConnector(Protocol):
    """One authorized source, for DSR purposes.

    Implementations are constructed with an authorization record and a credential
    that has ALREADY been resolved from the secret store -- a connector never reads
    the environment, never logs its credential, and never receives one that the
    authorization did not call for.
    """

    connector_name: str

    async def test_connection(self) -> bool: ...

    async def search_subject(
        self, *, identifiers: dict[str, str], limit: int = MAX_ROWS_PER_TABLE
    ) -> SearchOutcome:
        """Find records belonging to the subject, using the configured identifier
        columns only. `identifiers` maps an identifier kind ('email', 'phone',
        'reference') to the requester's value; the value is always bound as a
        parameter, never interpolated."""
        ...

    async def execute_action(
        self, *, table_name: str, record_reference: dict[str, Any], operation: str,
        payload: dict[str, Any],
    ) -> ExecutionOutcome:
        """Perform ONE approved operation against ONE record, then verify it by
        reading the record back. Raises rather than reporting a partial success."""
        ...


_REGISTRY: dict[str, type] = {}


def register(connector_name: str, connector_cls: type) -> None:
    _REGISTRY[connector_name] = connector_cls


def get_connector_class(connector_name: str) -> type:
    try:
        return _REGISTRY[connector_name]
    except KeyError:
        raise SourceNotAuthorizedError(
            f"no DSR connector registered for {connector_name!r}; "
            f"registered: {sorted(_REGISTRY)}"
        ) from None


def registered_connectors() -> list[str]:
    return sorted(_REGISTRY)
