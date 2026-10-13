"""Wire contract for POST /api/v1/dsr/requests/{id}/evidence (push evidence).

Consiva already holds the customer's database credentials and is willing to run
the lookup itself; this is what it POSTs back instead of handing the agent a
connection. Unlike ROPA's `/evidence` (app/agents/ropa/schemas/evidence.py), which
carries SCHEMA metadata only, this carries actual matched ROW VALUES -- personal
data -- so every limit here exists to bound how much of it one request can submit,
and `identifier_columns`/`record_key_columns`/`columns` exist so the agent never
stores or acts on a column Consiva did not explicitly name.

Size/count limits are deliberately NOT enforced as field_validators here. A
Pydantic ValueError becomes a 422 (see app/main.py's RequestValidationError
handler), and the spec this schema implements requires 413 for "too large" --
a distinct outcome from "malformed". Those checks live in
app/agents/dsr/services/evidence_push_service.py, which raises
EvidenceTooLargeError (413) explicitly; see MAX_TABLES_PER_PUSH,
MAX_ROWS_PER_TABLE, MAX_ROWS_PER_PUSH, MAX_PUSH_PAYLOAD_BYTES there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agents.dsr.connectors.authorization import IDENTIFIER_KINDS
from app.agents.dsr.connectors.base import validate_identifier
from app.agents.ropa.schemas.payload import clean_source_name


class PushedSubject(BaseModel):
    """What was searched for. At least one identifier is required -- the same rule
    `case_service.create_case` already applies to a case's own requester identifiers,
    since a search with none is not a search."""

    email: str | None = Field(default=None, max_length=320, description="The identifier searched by.")
    phone: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(
        default=None, max_length=200,
        description="A customer/employee/account id -- whichever identifier was used.",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> PushedSubject:
        if not any((self.email, self.phone, self.reference)):
            raise ValueError("subject needs at least one of email, phone or reference")
        return self


class PushedTable(BaseModel):
    """The matched rows from ONE table, exactly as Consiva's own lookup returned
    them -- never re-queried or re-matched by this agent."""

    table_name: str = Field(min_length=1, max_length=63, description="The table searched.")
    match_count: int = Field(
        ge=0,
        description="Total rows matched in this table -- may exceed len(rows) only when truncated=true.",
    )
    # The columns INCLUDED in `rows`. Every row's keys must be a subset of this --
    # an unlisted column is REJECTED (not silently dropped): Consiva, not this
    # agent, decides what it is willing to expose, and a key appearing in a row but
    # not declared here is either a bug in the sender or an attempt to smuggle a
    # column past the allowlist this field represents.
    columns: list[str] = Field(
        min_length=1, max_length=500,
        description="Columns included in `rows`. A row naming any other column is rejected.",
    )
    # {column: identifier_kind}, e.g. {"email": "email"}. At least one entry: every
    # matched row must be attributable to SOME identifier column, because
    # `dsr_evidence.matched_column`/`identifier_kind` are not-null (migration 0011).
    identifier_columns: dict[str, str] = Field(
        min_length=1, max_length=20,
        description='{column: identifier_kind}, e.g. {"email": "email"}; kind is one of '
                    '"email", "phone", "reference".',
    )
    # How a matched record is addressed later (an execution manifest entry's
    # record_reference). Required and non-empty for the same reason
    # connectors/authorization.py's resolve() requires it for a stored
    # authorization: a record with no locator cannot be addressed.
    record_key_columns: list[str] = Field(
        min_length=1, max_length=10,
        description="Columns that uniquely locate a row, for later execution (e.g. [\"id\"]).",
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Only the matched rows, max MAX_ROWS_PER_TABLE (500) -- see evidence_push_service.",
    )
    truncated: bool = Field(
        default=False,
        description="True if more rows matched than were sent; routes the case to REVIEW_REQUIRED.",
    )

    @field_validator("table_name")
    @classmethod
    def _valid_table(cls, value: str) -> str:
        return validate_identifier(value, kind="table")

    @field_validator("columns")
    @classmethod
    def _valid_columns(cls, value: list[str]) -> list[str]:
        return [validate_identifier(c, kind="column") for c in value]

    @model_validator(mode="after")
    def _consistent(self) -> PushedTable:
        column_set = set(self.columns)

        unknown_identifier_columns = set(self.identifier_columns) - column_set
        if unknown_identifier_columns:
            raise ValueError(
                f"{self.table_name}: identifier_columns names "
                f"{sorted(unknown_identifier_columns)}, which are not in columns"
            )
        unknown_kinds = sorted(set(self.identifier_columns.values()) - IDENTIFIER_KINDS)
        if unknown_kinds:
            raise ValueError(
                f"{self.table_name}: identifier_columns names identifier kind(s) "
                f"{unknown_kinds}, expected one of {sorted(IDENTIFIER_KINDS)}"
            )

        for column in self.record_key_columns:
            validate_identifier(column, kind="column")
        unknown_key_columns = set(self.record_key_columns) - column_set
        if unknown_key_columns:
            raise ValueError(
                f"{self.table_name}: record_key_columns names "
                f"{sorted(unknown_key_columns)}, which are not in columns"
            )

        for row in self.rows:
            extra = set(row) - column_set
            if extra:
                # Reject, never ignore: a row carrying a column Consiva did not
                # declare is either a sender bug or an attempt to smuggle a field
                # past the allowlist `columns` represents.
                raise ValueError(
                    f"{self.table_name}: a row has column(s) {sorted(extra)} not "
                    "declared in this table's columns"
                )
            missing_key = [c for c in self.record_key_columns if c not in row]
            if missing_key:
                raise ValueError(
                    f"{self.table_name}: a row is missing record_key_columns "
                    f"{missing_key}, needed to address it later"
                )

        if not self.truncated and self.match_count != len(self.rows):
            raise ValueError(
                f"{self.table_name}: match_count={self.match_count} but {len(self.rows)} "
                "row(s) were sent and truncated=false; they must agree when nothing was cut"
            )
        if self.truncated and self.match_count < len(self.rows):
            raise ValueError(
                f"{self.table_name}: match_count={self.match_count} is less than the "
                f"{len(self.rows)} row(s) sent"
            )
        return self


class EvidencePush(BaseModel):
    """The full body of POST /requests/{id}/evidence."""

    source_name: str = Field(
        min_length=1, max_length=200,
        description="Label for the system searched. No registered data source is required.",
    )
    searched_at: datetime | None = Field(default=None, description="When Consiva ran the search.")
    subject: PushedSubject = Field(description="What was searched for.")
    # No max_length here: the documented, enforced ceiling is
    # evidence_push_service.MAX_TABLES_PER_PUSH, checked explicitly so an
    # over-limit push gets 413 (never Pydantic's 422) -- see this module's
    # docstring.
    tables: list[PushedTable] = Field(
        default_factory=list,
        description="One entry per table searched; empty means searched, nothing matched.",
    )
    truncated: bool = Field(
        default=False, description="True if any part of the search was cut short overall.",
    )
    # Idempotency key. A retried push with the same value returns the SAME search
    # run and plan rather than re-ingesting and re-planning.
    client_reference: str | None = Field(
        default=None, max_length=200,
        description="Idempotency key. A retried push with the same value is a no-op.",
    )

    @field_validator("source_name")
    @classmethod
    def _meaningful_source_name(cls, value: str) -> str:
        cleaned = clean_source_name(value)
        if cleaned is None:
            raise ValueError("source_name must contain at least one letter or digit")
        return cleaned
