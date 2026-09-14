"""Subject search across authorized sources (prompt §17, §18, §19).

The order of operations here is the point:

    1. Identity gate    -- assert_identity_satisfied, before anything is read.
    2. Authorized only  -- sources come from dsr_source_authorizations, never from
                           the request, a URL, or a model.
    3. Deterministic    -- exact match on configured identifier columns only.
    4. Evidence         -- every match becomes a dsr_evidence row before any
                           conclusion is drawn from it.
    5. Explicit endings -- no match, multiple matches and connector failure are all
                           recorded outcomes with error codes, never silence.

A source that fails does not abort the whole search. Each source gets its own
`dsr_search_runs` row, so "we searched three systems, two answered, one was down"
is a state the case can actually represent -- which is what a DSR response has to
say honestly rather than reporting a clean "no data found".
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.connectors import factory
from app.agents.dsr.errors import DsrError, SearchFailedError, SourceNotAuthorizedError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import identity_service
from app.agents.ropa.rules import personal_data_rules
from app.db.models import DsrEvidence, DsrRequest, DsrSearchRun
from app.db.repositories import dsr_repository, ropa_repository

logger = logging.getLogger(__name__)


def _ropa_category(table_name: str, column_name: str) -> str | None:
    """Label a matched column with Agent 2's data category (blueprint §22).

    Uses Agent 2's CLASSIFIER rather than reading its stored ROPA records, and the
    difference matters. A stored record describes the schema as it stood at the last
    discovery run; the classifier describes the column actually found by this search.
    A DSR decision taken against stale metadata is a decision about data that may no
    longer be shaped that way, so the labelling is applied to what is in front of us.

    Best-effort by design: a column the rules cannot place returns None and the
    evidence simply carries no category. A label is useful context for a reviewer, not
    a fact the case depends on -- the record reference and snapshot are the evidence.
    """
    try:
        result = personal_data_rules.classify_column(column_name, table_name=table_name)
    except (ValueError, TypeError, AttributeError):
        # The classifier is pure and total, so this should not happen -- but a
        # labelling failure must never fail a search, and the label is context for a
        # reviewer rather than a fact the case depends on.
        logger.debug("could not classify %s.%s for a ROPA label", table_name, column_name)
        return None
    return result.category if result.is_personal_data else None


def requester_identifiers(request: DsrRequest) -> dict[str, str]:
    """The identifiers this case may be searched on. Only these three: a DSR search
    matches a person by an identifier they gave, not by name similarity."""
    return {
        kind: value.strip()
        for kind, value in (
            ("email", request.requester_email),
            ("phone", request.requester_phone),
            ("reference", request.requester_reference),
        )
        if value and value.strip()
    }


class SearchSummary:
    """What the whole search established, across every source."""

    def __init__(self):
        self.runs: list[DsrSearchRun] = []
        self.evidence_count = 0
        self.distinct_subjects = 0
        self.sources_failed: list[str] = []
        self.sources_searched: list[str] = []
        self.ambiguous = False
        self.notes: list[str] = []

    @property
    def found_anything(self) -> bool:
        return self.evidence_count > 0

    @property
    def outcome_code(self) -> str | None:
        """The single code that best describes how the search ended, or None when it
        ended cleanly with matches."""
        if self.ambiguous:
            return case.ERR_MULTIPLE_MATCHES
        if not self.found_anything and self.sources_failed:
            return case.ERR_SEARCH_FAILED
        if not self.found_anything:
            return case.ERR_NO_MATCH
        return None


async def run_search(
    db: AsyncSession,
    request: DsrRequest,
    *,
    job_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> SearchSummary:
    """Search every authorized source for this case's requester.

    Raises only if the search could not be attempted at all (identity not verified,
    no identifiers, no authorized source). A source-level failure is recorded on that
    source's run and the search continues.
    """
    # 1. The gate. Re-read from the database rather than trusting request.status --
    #    a case whose status was advanced elsewhere still cannot search without a
    #    real, unexpired verification row behind it.
    await identity_service.assert_identity_satisfied(db, request)

    identifiers = requester_identifiers(request)
    if not identifiers:
        raise SearchFailedError(
            f"case {request.reference} carries no requester identifier to search on"
        )

    authorizations = await dsr_repository.list_source_authorizations(db, request.org_id)
    if not authorizations:
        raise SourceNotAuthorizedError(
            "no data source is authorized for DSR in this organization; "
            "register one before running a search"
        )

    summary = SearchSummary()
    for authorization in authorizations:
        data_source = await ropa_repository.get_data_source(
            db, authorization.data_source_id, request.org_id
        )
        if data_source is None or not data_source.enabled:
            # The authorization outlived the source it points at. Recorded, not
            # skipped silently.
            summary.notes.append(
                f"authorization {authorization.id} points at a missing or disabled source"
            )
            continue
        await _search_one_source(
            db, request, authorization, data_source, identifiers, summary,
            job_id=job_id, correlation_id=correlation_id,
        )

    return summary


async def _search_one_source(
    db: AsyncSession,
    request: DsrRequest,
    authorization,
    data_source,
    identifiers: dict[str, str],
    summary: SearchSummary,
    *,
    job_id: uuid.UUID | None,
    correlation_id: str | None,
) -> None:
    run = await dsr_repository.create_search_run(
        db,
        org_id=request.org_id,
        request_id=request.id,
        source_name=data_source.name,
        data_source_id=data_source.id,
        identifier_kinds=sorted(identifiers),
        job_id=job_id,
        correlation_id=correlation_id,
    )
    summary.runs.append(run)
    run.status = "running"
    await db.flush()

    try:
        connector = factory.build_connector(
            data_source=data_source, authorization=authorization, for_execution=False,
        )
        outcome = await connector.search_subject(identifiers=identifiers)
    except DsrError as exc:
        # An expected, classified failure: connector down, timeout, misconfiguration.
        # The case is not failed here -- other sources may still answer -- but this
        # source's run carries the code, so the response can say what was not searched.
        run.status = "failed"
        run.error_code = exc.code or case.ERR_SEARCH_FAILED
        run.error_detail = exc.message
        run.completed_at = datetime.now(UTC)
        summary.sources_failed.append(data_source.name)
        await db.flush()
        logger.warning(
            "DSR search failed for case %s on source %s: %s",
            request.reference, data_source.name, exc.code,
        )
        return
    except Exception as exc:
        # An UNEXPECTED failure. Recorded with the same explicitness rather than
        # allowed to propagate and kill the whole multi-source search, but logged at
        # exception level because it is a bug, not an operational condition.
        run.status = "failed"
        run.error_code = case.ERR_SEARCH_FAILED
        run.error_detail = f"{type(exc).__name__} during search"
        run.completed_at = datetime.now(UTC)
        summary.sources_failed.append(data_source.name)
        await db.flush()
        logger.exception(
            "Unexpected error searching source %s for case %s", data_source.name, request.reference
        )
        return

    evidence_rows = [
        DsrEvidence(
            org_id=request.org_id,
            request_id=request.id,
            search_run_id=run.id,
            data_source_id=data_source.id,
            source_name=data_source.name,
            table_name=match.table_name,
            schema_name=match.schema_name,
            matched_column=match.matched_column,
            identifier_kind=match.identifier_kind,
            match_type=match.match_type,
            confidence=match.confidence,
            record_reference=match.record_reference,
            record_snapshot=match.record_snapshot,
            ropa_category=_ropa_category(match.table_name, match.matched_column),
        )
        for match in outcome.matches
    ]
    await dsr_repository.add_evidence(db, request.org_id, evidence_rows)

    run.match_count = len(evidence_rows)
    run.distinct_subject_count = outcome.distinct_subjects
    run.tables_searched = list(outcome.tables_searched)
    run.completed_at = datetime.now(UTC)

    if outcome.distinct_subjects > 1:
        run.status = "multiple_matches"
        run.error_code = case.ERR_MULTIPLE_MATCHES
        run.error_detail = (
            f"{outcome.distinct_subjects} distinct subjects matched the supplied "
            "identifier; a human must confirm which one the requester is"
        )
        summary.ambiguous = True
    elif not evidence_rows:
        run.status = "no_match"
        run.error_code = case.ERR_NO_MATCH
        run.error_detail = "no record matched the supplied identifiers in this source"
    else:
        run.status = "completed"

    if outcome.truncated:
        # More rows existed than the cap allows. Never treated as a complete result:
        # a partial answer presented as complete is a wrong answer.
        summary.ambiguous = True
        run.error_code = case.ERR_MULTIPLE_MATCHES
        run.error_detail = (
            "the identifier matched more rows than the per-table cap; "
            "the result is incomplete and must be reviewed"
        )

    await db.flush()

    summary.evidence_count += len(evidence_rows)
    summary.distinct_subjects = max(summary.distinct_subjects, outcome.distinct_subjects)
    summary.sources_searched.append(data_source.name)
    summary.notes.extend(f"{data_source.name}: {note}" for note in outcome.notes)
