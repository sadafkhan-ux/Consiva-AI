"""Building blocks for "push evidence": Consiva runs the subject search itself
(it already holds the customer's database credentials) and POSTs the matched
records here, instead of this agent connecting to the customer's database
directly (POST /requests/{id}/search, which fails SOURCE_NOT_AUTHORIZED for an
org with no registered dsr_source_authorizations row).

This module holds the PURE, schema-aware pieces -- limits, the identity/state
gate, turning a pushed table into a SourceGrant and a batch of DsrEvidence rows,
and encrypting the raw payload at rest. The ORCHESTRATION (create the search run,
call planning_service.build_plan, move the case through its transitions, write
the audit trail) lives in app/services/dsr_run_service.py, right next to
execute_queued_search -- the pull-mode counterpart this mirrors -- for the same
reason that module already sits apart from app/agents/dsr/services/: it is the
seam between the platform (sessions, transactions, the case lifecycle) and the
agent's own logic, not the agent's logic itself.

PRIVACY (push-evidence addendum, point 4)
------------------------------------------
Rows are personal data. Nothing here ever logs a row value -- only table names,
column names and counts (see dsr_run_service.ingest_pushed_evidence's own log
line). The raw pushed payload is encrypted at rest (Fernet, the SAME key and
functions ROPA already uses for a stored credential -- see
app/agents/ropa/connectors/factory.py's module docstring: "one Fernet master key
per deployment for anything sensitive at rest is simpler than a key per use
case" -- this is that same case, applied to a different kind of secret). Only
the columns Consiva lists are ever stored or acted on; an unlisted column is
rejected at the schema boundary (app/agents/dsr/schemas/evidence_push.py).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.connectors import base as connector_base
from app.agents.dsr.connectors.authorization import SourceGrant
from app.agents.dsr.errors import CaseNotReadyError, EvidenceTooLargeError
from app.agents.dsr.schemas import case
from app.agents.dsr.schemas.evidence_push import EvidencePush, PushedSubject, PushedTable
from app.agents.dsr.services import identity_service, lifecycle
from app.agents.ropa.connectors import factory as credential_factory
from app.agents.ropa.rules import personal_data_rules
from app.db.models import DsrEvidence, DsrRequest, DsrSearchRun
from app.db.repositories import dsr_repository

# ── Documented limits (rejected whole with 413 -- never silently truncated) ──────
#
# Reuses the connector's own existing per-table cap (connectors/base.py), so a
# pushed table is held to the exact same ceiling a connector-driven search already
# enforces on itself.
MAX_ROWS_PER_TABLE = connector_base.MAX_ROWS_PER_TABLE  # 500
# A DSR push carries ONE subject's matched records across a handful of systems,
# not a whole schema. ROPA's push ceiling (MAX_TABLES_PER_PUSH=2000) bounds SCHEMA
# metadata for an entire database and is not a sensible scale reference here --
# every table in THIS push carries actual personal-data rows, not column metadata.
MAX_TABLES_PER_PUSH = 50
# Total rows across every table in one push -- the backstop for a push that stays
# within the per-table cap on every table but is enormous in aggregate.
MAX_ROWS_PER_PUSH = 5_000
# The practical DoS ceiling for the whole request body, checked on the RAW bytes
# before the body is parsed into EvidencePush at all (routes/dsr.py).
MAX_PUSH_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB


def enforce_limits(push: EvidencePush) -> None:
    """Raise EvidenceTooLargeError (413) the moment any documented ceiling is
    exceeded. Called BEFORE anything is written -- a push that is going to be
    refused must not leave a half-ingested search run behind."""
    if len(push.tables) > MAX_TABLES_PER_PUSH:
        raise EvidenceTooLargeError(
            f"push has {len(push.tables)} table(s); max {MAX_TABLES_PER_PUSH} per push"
        )
    total_rows = 0
    for table in push.tables:
        if len(table.rows) > MAX_ROWS_PER_TABLE:
            raise EvidenceTooLargeError(
                f"table {table.table_name!r} has {len(table.rows)} row(s); "
                f"max {MAX_ROWS_PER_TABLE} per table"
            )
        total_rows += len(table.rows)
    if total_rows > MAX_ROWS_PER_PUSH:
        raise EvidenceTooLargeError(
            f"push has {total_rows} row(s) across all tables; max {MAX_ROWS_PER_PUSH} per push"
        )


# ── The gate: same identity check as /search, same "can reach SEARCHING" check ──
# as /search, both answered with 409 (CaseNotReadyError) rather than /search's
# split between IdentityRequiredError/IdentityFailedError (403) and
# InvalidCaseTransitionError (409) -- this endpoint's own documented contract.

#: Every status from which a case may legally move to SEARCHING, `failed`
#: included so a case that could never run a connector search (no registered
#: source -- SOURCE_NOT_AUTHORIZED) can still be recovered via a push.
STATES_THAT_MAY_RECEIVE_PUSHED_EVIDENCE = frozenset(
    status for status in case.ALL_STATUSES if lifecycle.can_transition(status, case.SEARCHING)
)


async def assert_case_accepts_push(db: AsyncSession, request: DsrRequest) -> None:
    if request.status not in STATES_THAT_MAY_RECEIVE_PUSHED_EVIDENCE:
        raise CaseNotReadyError(
            f"case {request.reference} is {request.status!r}; evidence may only be pushed "
            f"for a case in one of {sorted(STATES_THAT_MAY_RECEIVE_PUSHED_EVIDENCE)}"
        )
    verification = await dsr_repository.get_latest_identity_verification(db, request.id, request.org_id)
    status = identity_service.effective_status(verification)
    if status not in case.IDV_SATISFIED:
        raise CaseNotReadyError(
            f"case {request.reference} has not completed identity verification "
            f"(status: {status}); evidence may not be pushed until it does"
        )


def assert_subject_matches_case(request: DsrRequest, subject: PushedSubject) -> None:
    """The pushed subject must name the SAME person this case's identity gate
    already verified. Consiva did the matching, not this agent, so this is the one
    check standing between a sender's bug and a different person's records landing
    on this case."""
    pairs = (
        (request.requester_email, subject.email),
        (request.requester_phone, subject.phone),
        (request.requester_reference, subject.reference),
    )
    matched = any(
        case_value and pushed_value and case_value.strip().lower() == pushed_value.strip().lower()
        for case_value, pushed_value in pairs
    )
    if not matched:
        raise CaseNotReadyError(
            f"case {request.reference}: the pushed subject matches none of this case's "
            "requester identifiers (email/phone/reference); refusing to attach it"
        )


# ── Turning a push into what the engine already knows how to run ────────────────

def grant_for_push(push: EvidencePush) -> SourceGrant:
    """A SourceGrant synthesized directly from what Consiva declared, so
    planning_service.build_plan can classify, apply retention and build a plan
    exactly as it does for a connector-driven search -- there is no
    dsr_source_authorizations row behind a pushed source (often there is no
    registered data source at all; that is the whole reason this path exists).

    `allow_execution=True` with `external_execution=True` and no
    `write_credential_ref`: planning may propose a mutating action (this agent
    never writes to the source either way), but the constraint engine's write-
    credential check is waived because there genuinely is no write credential
    for THIS process to hold -- Consiva executes it and reports back via
    POST /requests/{id}/execution-report. `returnable_columns`/`erasable_columns`
    are both exactly the columns Consiva listed per table: it already decided
    what it is willing to expose and, by extension, what it is willing to act on.
    """
    returnable = {t.table_name: tuple(t.columns) for t in push.tables}
    return SourceGrant(
        source_name=push.source_name,
        searchable_tables=tuple(t.table_name for t in push.tables),
        identity_tables=frozenset(),
        identifier_columns={},
        returnable_columns=returnable,
        record_key_columns={t.table_name: tuple(t.record_key_columns) for t in push.tables},
        erasable_columns=returnable,
        allow_execution=True,
        write_credential_ref=None,
        external_execution=True,
    )


def _matched_column_and_kind(table: PushedTable) -> tuple[str, str]:
    """The first declared {column: kind} pair. dsr_evidence stores exactly one
    matched_column/identifier_kind per row (both not-null, migration 0011); a
    table may declare more than one identifier column, but one evidence row can
    only cite one, so the first (dict insertion order, as Consiva sent it) wins."""
    column, kind = next(iter(table.identifier_columns.items()))
    return column, kind


def category_for(table_name: str, column_name: str) -> str | None:
    """Agent 2's data category for a matched column, best-effort -- the same rule
    search_service._ropa_category applies for a connector-driven match. A column
    the classifier cannot place returns None; that is a normal answer, not a bug."""
    try:
        result = personal_data_rules.classify_column(column_name, table_name=table_name)
    except (ValueError, TypeError, AttributeError):
        return None
    return result.category if result.is_personal_data else None


def evidence_rows_for_push(request: DsrRequest, run: DsrSearchRun, push: EvidencePush) -> list[DsrEvidence]:
    rows: list[DsrEvidence] = []
    for table in push.tables:
        matched_column, identifier_kind = _matched_column_and_kind(table)
        for raw_row in table.rows:
            record_reference = {column: raw_row.get(column) for column in table.record_key_columns}
            rows.append(DsrEvidence(
                org_id=request.org_id, request_id=request.id, search_run_id=run.id,
                data_source_id=None, source_name=push.source_name, schema_name=None,
                table_name=table.table_name, matched_column=matched_column,
                identifier_kind=identifier_kind,
                # Consiva already matched these via its own lookup; this agent has
                # no finer-grained signal than "it matched", so every pushed row is
                # recorded as an exact match.
                match_type="exact", confidence=1.0,
                record_reference=record_reference, record_snapshot=dict(raw_row),
                ropa_category=category_for(table.table_name, matched_column),
            ))
    return rows


# ── Encrypting the raw payload at rest (purgeable independently of dsr_evidence) ─

def encrypt_push_payload(push: EvidencePush) -> str:
    """Fernet-encrypt exactly the pushed `tables` array -- the bulk row content --
    for storage in dsr_evidence_pushes.payload_ciphertext (migration 0035). Reuses
    ROPA's existing credential encryption (ROPA_CREDENTIAL_ENCRYPTION_KEY): one
    master key per deployment for anything sensitive at rest, not a new key per
    use case (see app/agents/ropa/connectors/factory.py)."""
    raw = json.dumps([t.model_dump(mode="json") for t in push.tables], default=str)
    return credential_factory.encrypt_credential(raw)


def decrypt_push_payload(ciphertext: str) -> list[dict]:
    """The inverse of `encrypt_push_payload`, for the rare case something needs the
    raw pushed tables back (e.g. a support investigation) before the evidence is
    purged. Nothing in the push-evidence request path calls this -- planning and
    the engine work from the already-derived dsr_evidence rows, never from this
    ciphertext."""
    return json.loads(credential_factory.decrypt_credential(ciphertext))


async def purge(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> int:
    """Clear the encrypted raw payload for every not-yet-purged push on this case.
    `dsr_evidence` (the minimized, classified rows the engine reasons over) is left
    exactly as it is -- it keeps living under the case's existing retention, same
    as a connector-driven search's evidence always has."""
    return await dsr_repository.purge_evidence_pushes(db, request_id, org_id)


def search_run_identifier_kinds(push: EvidencePush) -> list[str]:
    return sorted({kind for table in push.tables for kind in table.identifier_columns.values()})
