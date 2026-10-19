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

import json
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


def _record_key(source_name: str, table_name: str, record_reference: dict) -> tuple[str, str, str]:
    """A stable, hashable identity for one matched record: the same (source, table,
    record_reference) a prior search attempt on this SAME case already turned into a
    `dsr_evidence` row. A case may be searched more than once -- a connector failure
    retried after the source is fixed, an ambiguous result retried after review --
    and `dsr_search_runs`/`dsr_evidence` are append-only history, so nothing deletes
    or supersedes an earlier attempt's rows. Without this, a record that matches
    again on a second attempt becomes a SECOND `dsr_evidence` row for the exact same
    underlying record, and `planning_service.build_plan` (which reads every
    evidence row ever written for the case) then plans -- and could execute -- the
    same disclosure or deletion twice.

    `record_reference` is serialized rather than compared as a dict because a dict
    is not hashable; `default=str` tolerates a value json can't natively encode
    (e.g. a UUID or date) the same way encrypt_push_payload already does.
    """
    return (source_name, table_name, json.dumps(record_reference, sort_keys=True, default=str))


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
        ended cleanly with matches AND every authorized source actually answered.

        A source failing is never folded into a clean ending just because another
        source found something: "we found 2 records, and could not reach a third
        system at all" is a materially different outcome from "we found 2 records,
        full stop", and a reviewer (or an automated retry) reading only the case's
        own error_code -- not every dsr_search_runs row -- must be able to tell
        them apart. See dsr_run_service._search_detail, which composes the matching
        human-readable text for this same code.
        """
        if self.ambiguous:
            return case.ERR_MULTIPLE_MATCHES
        if self.sources_failed:
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
            "no data source is connected for DSR in this organisation. A source registered "
            "for ROPA discovery is not usable by DSR until an administrator authorises it "
            "under DSR \u2192 Configuration"
        )

    # Every authorization's source belongs to this same org, so one list call covers
    # all of them -- looking each one up individually cost N+1 queries for no reason
    # (same fix as the /config/sources route; see its docstring).
    sources_by_id = {
        s.id: s for s in await ropa_repository.list_data_sources(db, request.org_id)
    }

    # Every record this case already has evidence for, from THIS search or any
    # earlier attempt -- fetched once, up front, rather than once per source. A
    # retry (connector failure fixed, ambiguity reviewed) must not re-litigate a
    # record it already evidenced; see `_record_key`'s docstring.
    existing_evidence_keys = {
        _record_key(e.source_name, e.table_name, e.record_reference)
        for e in await dsr_repository.list_evidence(db, request.id, request.org_id)
    }

    summary = SearchSummary()
    for authorization in authorizations:
        data_source = sources_by_id.get(authorization.data_source_id)
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
            existing_evidence_keys=existing_evidence_keys,
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
    existing_evidence_keys: set[tuple[str, str, str]] | None = None,
) -> None:
    # Optional and defaulted to None (treated as empty) rather than required: a
    # caller driving one source in isolation (outside `run_search`, e.g. a focused
    # test) has no case-wide evidence history to check and should see unchanged,
    # undeduplicated behaviour rather than an AttributeError.
    if existing_evidence_keys is None:
        existing_evidence_keys = set()
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

    try:
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

        # Insert only the rows this case does not already have evidence for. A
        # record already evidenced by an earlier attempt on this same case is not
        # re-inserted -- `run.match_count` below still reports the full number of
        # records THIS run matched (a truthful account of what this run found), but
        # `dsr_evidence` itself gains at most one row per real-world record, so
        # planning never double-plans (and could double-execute) the same
        # disclosure or deletion. See `_record_key`'s docstring.
        rows_to_insert = []
        duplicate_count = 0
        for row in evidence_rows:
            key = _record_key(row.source_name, row.table_name, row.record_reference)
            if key in existing_evidence_keys:
                duplicate_count += 1
                continue
            existing_evidence_keys.add(key)
            rows_to_insert.append(row)
        await dsr_repository.add_evidence(db, request.org_id, rows_to_insert)
        if duplicate_count:
            summary.notes.append(
                f"{data_source.name}: {duplicate_count} matched record(s) were already "
                "recorded as evidence by an earlier search on this case and were not "
                "duplicated"
            )

        run.match_count = len(evidence_rows)
        run.distinct_subject_count = outcome.distinct_subjects
        run.tables_searched = list(outcome.tables_searched)

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

        # A source with no identity table configured reports one "distinct subject" for
        # any number of matches, because it cannot tell -- the connector says so, and
        # leaves it to this service to treat that as a review signal. Taking the 1 at
        # face value made the multiple-subject guard inert on every default-configured
        # source: records that may belong to two different people went straight to
        # planning. getattr default True: a connector that does not expose the property
        # makes no such disclaimer.
        if evidence_rows and not getattr(connector, "has_identity_table", True):
            summary.ambiguous = True
            run.error_code = case.ERR_MULTIPLE_MATCHES
            run.error_detail = (
                "no identity table is configured for this source, so it cannot tell "
                "whether the matched records belong to one person; a human must confirm"
            )
            summary.notes.append(
                f"{data_source.name}: no identity_tables configured, so the number of "
                "distinct subjects matched is unknown -- configure identity_tables on "
                "this source's authorization to let a single-subject match proceed"
            )
    except Exception as exc:
        # Everything above ran after the connector call already succeeded, so this is
        # the same "unexpected failure" category the `except Exception` above the
        # connector call already handles -- an evidence-insert constraint violation, the
        # ROPA classifier, anything. Without this, the exception propagated past this
        # function to execute_queued_search's outer handler, which fails the CASE but
        # never touches THIS run -- leaving it at status="running" forever, indistinguishable
        # from a search that is still in flight on a case that is actually terminal.
        run.status = "failed"
        run.error_code = case.ERR_SEARCH_FAILED
        run.error_detail = f"{type(exc).__name__} while recording search results"
        run.completed_at = datetime.now(UTC)
        summary.sources_failed.append(data_source.name)
        await db.flush()
        logger.exception(
            "Unexpected error recording search results for source %s on case %s",
            data_source.name, request.reference,
        )
        return

    run.completed_at = datetime.now(UTC)
    await db.flush()

    summary.evidence_count += len(evidence_rows)
    summary.distinct_subjects = max(summary.distinct_subjects, outcome.distinct_subjects)
    summary.sources_searched.append(data_source.name)
    summary.notes.extend(f"{data_source.name}: {note}" for note in outcome.notes)
