"""Runs one Purpose Classifier assessment and persists what it found.

Follows the pattern every agent here except Agent 1 uses: an async service pipeline
driven by the existing worker, with no LangGraph. The plan is explicit that LangGraph
is not needed initially, and nothing in this flow pauses for a human mid-run -- review
happens after the findings exist, through the endpoints, not by suspending the graph.

The pipeline is deliberately thin. It gathers evidence two other modules already own,
hands each item to the comparison service, and writes the result. All the judgement
lives in `comparison_service`; all the reading lives in `observed_service` and
`declared_service`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.purpose.connectors import rest, structured
from app.agents.purpose.services import (
    comparison_service,
    declared_service,
    observed_service,
    retention_service,
    structured_service,
)
from app.core.exceptions import ConsivaError, NotFoundError
from app.db.models import ConsentScan, PurposeAssessment, PurposeFinding, PurposeRun
from app.db.session import async_session_factory, set_org_scope
from app.jobs import queue
from app.services import audit_service

logger = logging.getLogger(__name__)

JOB_TYPE = "purpose_assessment"


async def request_source_assessment(
    db: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID,
    source_name: str, connector: str, dsn: str | None = None,
    csv_content: str | None = None, table_name: str | None = None,
    spec_url: str | None = None, auth_header: str | None = None,
) -> PurposeRun:
    """Queue an assessment over a structured data source.

    The connection string -- or the API credential -- is passed through the job payload
    and is never written to the run record. It reaches the worker, is used once to read
    the schema, and is not retained: a DSN or bearer token stored on a row is a
    credential in a table that many people can read.
    """
    if connector not in ("postgres", "csv", "rest"):
        raise ConsivaError(
            f"Unsupported connector {connector!r}. Use 'postgres', 'csv' or 'rest'."
        )
    if connector == "postgres" and not (dsn or "").strip():
        raise ConsivaError("A connection string is required for the postgres connector.")
    if connector == "csv" and not (csv_content or "").strip():
        raise ConsivaError("File content is required for the csv connector.")
    if connector == "rest" and not (spec_url or "").strip():
        raise ConsivaError(
            "An OpenAPI specification URL is required for the rest connector. This "
            "agent reads the specification, never the API's records."
        )

    run = PurposeRun(org_id=org_id, scan_id=None, status="queued")
    db.add(run)
    await db.flush()

    await audit_service.record(
        db, org_id=org_id, actor_user_id=user_id,
        action="purpose.source_assessment.requested",
        entity_type="purpose_run", entity_id=run.id,
        # The source NAME is audited; the credential is not.
        after={"source_name": source_name, "connector": connector},
    )
    await queue.enqueue(
        db, org_id=org_id, job_type=JOB_TYPE,
        payload={
            "run_id": str(run.id), "org_id": str(org_id), "mode": "source",
            "source_name": source_name, "connector": connector,
            "dsn": dsn, "csv_content": csv_content, "table_name": table_name,
            "spec_url": spec_url, "auth_header": auth_header,
        },
    )
    await db.commit()
    logger.info(
        "purpose.source_assessment.queued run_id=%s org_id=%s connector=%s source=%s",
        run.id, org_id, connector, source_name,
    )
    return run


async def request_assessment(
    db: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, scan_id: uuid.UUID
) -> PurposeRun:
    """Create the run and queue the work. Returns immediately.

    The scan is verified to belong to the caller's organisation here rather than in the
    worker: a queued job carries an org_id it would otherwise be trusted to have got
    right, and the check belongs at the boundary where an authenticated user exists.
    """
    scan = (await db.execute(
        select(ConsentScan).where(ConsentScan.id == scan_id, ConsentScan.org_id == org_id)
    )).scalars().first()
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")

    run = PurposeRun(org_id=org_id, scan_id=scan_id, status="queued")
    db.add(run)
    await db.flush()

    await audit_service.record(
        db, org_id=org_id, actor_user_id=user_id,
        action="purpose.assessment.requested", entity_type="purpose_run", entity_id=run.id,
    )
    await queue.enqueue(
        db, org_id=org_id, job_type=JOB_TYPE,
        payload={"run_id": str(run.id), "org_id": str(org_id), "scan_id": str(scan_id)},
    )
    await db.commit()
    logger.info(
        "purpose.assessment.queued run_id=%s org_id=%s scan_id=%s", run.id, org_id, scan_id
    )
    return run


async def execute_job(payload: dict) -> None:
    """Entry point for jobs/worker.py.

    One job type, two input modes. They differ only in where the observed purposes come
    from -- everything after that (comparison, retention, findings, persistence) is
    identical, which is the point: the agent's judgement must not depend on which door
    the evidence came through.
    """
    run_id = uuid.UUID(payload["run_id"])
    org_id = uuid.UUID(payload["org_id"])
    # Bound once for the whole job, before any session opens. Everything below runs
    # outside a request, so nothing else sets it -- and since migration 0028 the three
    # purpose tables carry FORCE row-level security, which means an unscoped worker
    # would read its own run back as missing and fail every job it was handed.
    #
    # set_org_scope, not bind_org_scope: there is no open transaction here to stamp.
    # The after_begin hook in db/session.py applies it to each transaction as it starts.
    set_org_scope(org_id)
    if payload.get("mode") == "source":
        await execute_source(run_id, org_id, payload)
    else:
        await execute(run_id, org_id, uuid.UUID(payload["scan_id"]))


async def execute_source(run_id: uuid.UUID, org_id: uuid.UUID, payload: dict) -> None:
    """Assess a structured data source: read its shape, classify, compare."""
    if not await _mark_running(run_id):
        return
    try:
        connector = payload["connector"]
        source_name = payload["source_name"]

        if connector == "postgres":
            schema = await structured.read_postgres(payload["dsn"], source_name=source_name)
        elif connector == "rest":
            # The SPECIFICATION, not the API. See connectors/rest.py: calling the
            # endpoints would mean this agent fetching the very personal data it exists
            # to ask questions about.
            schema = await rest.read_openapi(
                payload["spec_url"], source_name=source_name,
                auth_header=payload.get("auth_header"),
            )
        else:
            schema = structured.read_csv(
                payload["csv_content"], source_name=source_name,
                table_name=payload.get("table_name"),
            )

        observed = structured_service.observe(schema)

        async with async_session_factory() as db:
            declared = await declared_service.for_org(db, org_id)
            # Declarations name `table.column`; observations name `table`. Without
            # this re-keying the two never meet and every comparison is undetermined
            # for a reason that has nothing to do with the data.
            declared = structured_service.index_declared_by_table(declared)

            assessments = [
                _with_retention(comparison_service.assess(item, declared, None), item)
                for item in observed
            ]
            counts = await _persist(
                db, run_id=run_id, org_id=org_id, scan_id=None, assessments=assessments
            )

            run = await db.get(PurposeRun, run_id)
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.assessments_count = counts["assessments"]
            run.findings_count = counts["findings"]
            notes = []
            if schema.truncated:
                notes.append(
                    "The schema was larger than this run reads; results cover the first "
                    "tables only."
                )
            skipped = len(schema.tables) - len(observed)
            if skipped > 0:
                # Said out loud: a reviewer seeing 3 assessments from a 40-table
                # database needs to know the other 37 were skipped deliberately.
                notes.append(
                    f"{skipped} of {len(schema.tables)} tables showed no personal-data "
                    f"columns and were not assessed."
                )
            note = declared_service.availability_note(declared)
            if note:
                notes.append(note)
            run.error = " ".join(notes) or None
            await db.commit()

        logger.info(
            "purpose.source_assessment.completed run_id=%s tables=%d assessed=%d findings=%d",
            run_id, len(schema.tables), counts["assessments"], counts["findings"],
        )
    except Exception as exc:
        await _mark_failed(run_id, exc)
        raise


def _with_retention(assessment, item):
    status, note = retention_service.evaluate(item)
    assessment.retention_status = status
    assessment.retention_note = note
    if status == retention_service.REVIEW_REQUIRED and assessment.finding is None:
        assessment.finding = comparison_service.Finding(
            finding_type=comparison_service.RETENTION,
            severity="low",
            title=f"Retention worth reviewing: {item.subject_label}",
            description=note or "",
        )
    return assessment


async def _mark_running(run_id: uuid.UUID) -> bool:
    """Claim a run for execution. False means it has already been done -- do not re-run.

    A queued job is retried up to three times (jobs/queue.py), and `reap_stale_jobs`
    requeues one whose worker died. So a run CAN be handed to execution twice: once
    after writing its assessments but before the status commit landed, and once again
    on the retry. Nothing checked for that, and the result is silent duplication --
    every assessment and every finding written a second time, with `assessments_count`
    still reporting one pass.

    It is not theoretical. Seeding demo data executed a job inline while the worker
    also picked it up from the queue, and five runs came back with exactly twice the
    rows their own count field claimed. In production a compliance reviewer would see
    each finding twice and have no way to tell which was real.

    So: a run that already COMPLETED is left alone, and a run that was mid-flight has
    its partial output cleared before the retry writes again. Either way the run ends
    with exactly one set of rows.
    """
    async with async_session_factory() as db:
        run = await db.get(PurposeRun, run_id)
        if run is None:
            return False
        if run.status == "completed":
            logger.info(
                "purpose: run %s is already completed; not re-running it. The queued "
                "job was delivered twice.", run_id,
            )
            return False

        # A previous attempt may have written rows before failing. Findings go first:
        # they reference assessments.
        await db.execute(delete(PurposeFinding).where(
            PurposeFinding.assessment_id.in_(
                select(PurposeAssessment.id).where(PurposeAssessment.run_id == run_id)
            )
        ))
        await db.execute(delete(PurposeAssessment).where(PurposeAssessment.run_id == run_id))

        run.status = "running"
        run.started_at = datetime.now(UTC)
        run.assessments_count = 0
        run.findings_count = 0
        await db.commit()
        return True


async def _mark_failed(run_id: uuid.UUID, exc: Exception) -> None:
    logger.exception("purpose.assessment.failed run_id=%s", run_id)
    async with async_session_factory() as db:
        run = await db.get(PurposeRun, run_id)
        if run is not None:
            run.status = "failed"
            run.completed_at = datetime.now(UTC)
            run.error = str(exc)[:500]
            await db.commit()


async def execute(run_id: uuid.UUID, org_id: uuid.UUID, scan_id: uuid.UUID) -> None:
    """Assess a consent scan's evidence."""
    # Claimed through the same helper as the source path. This used to open-code the
    # status update, which meant it carried the identical double-execution gap and
    # would have had to be fixed twice.
    if not await _mark_running(run_id):
        return

    try:
        async with async_session_factory() as db:
            observed = await observed_service.collect_for_scan(db, scan_id)
            consent_obtained = await observed_service.consent_was_obtained(db, scan_id)
            declared = await declared_service.for_org(db, org_id)
            note = declared_service.availability_note(declared)

            assessments = []
            for item in observed:
                assessment = comparison_service.assess(item, declared, consent_obtained)
                status, retention_note = retention_service.evaluate(item)
                assessment.retention_status = status
                assessment.retention_note = retention_note
                # A retention prompt is a finding in its own right when nothing more
                # serious was already raised for this item -- it should not displace a
                # consent violation on the same cookie.
                if status == retention_service.REVIEW_REQUIRED and assessment.finding is None:
                    assessment.finding = comparison_service.Finding(
                        finding_type=comparison_service.RETENTION,
                        severity="low",
                        title=f"Retention worth reviewing: {item.subject_label}",
                        description=retention_note or "",
                    )
                assessments.append(assessment)

            counts = await _persist(
                db, run_id=run_id, org_id=org_id, scan_id=scan_id, assessments=assessments
            )

            run = await db.get(PurposeRun, run_id)
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.assessments_count = counts["assessments"]
            run.findings_count = counts["findings"]
            if note:
                # Recorded on the run so a reviewer seeing only `undetermined` results
                # knows whether the data was fine or half the comparison was missing.
                run.error = note
            await db.commit()

        logger.info(
            "purpose.assessment.completed run_id=%s assessments=%d findings=%d",
            run_id, counts["assessments"], counts["findings"],
        )
    except Exception as exc:
        # Recorded, then re-raised so the queue's retry and failure accounting apply
        # exactly as they do for every other job type.
        logger.exception("purpose.assessment.failed run_id=%s", run_id)
        async with async_session_factory() as db:
            run = await db.get(PurposeRun, run_id)
            if run is not None:
                run.status = "failed"
                run.completed_at = datetime.now(UTC)
                run.error = str(exc)[:500]
                await db.commit()
        raise


async def _persist(
    db: AsyncSession, *, run_id: uuid.UUID, org_id: uuid.UUID, scan_id: uuid.UUID,
    assessments: list,
) -> dict[str, int]:
    findings = 0
    for item in assessments:
        row = PurposeAssessment(
            org_id=org_id,
            subject_type=item.subject_type,
            subject_ref=item.subject_ref,
            subject_label=item.subject_label,
            declared_purpose=item.declared_purpose,
            declared_source=item.declared_source,
            observed_purpose=item.observed_purpose,
            observed_source=item.observed_source,
            alignment=item.alignment,
            confidence=item.confidence,
            retention_status=item.retention_status,
            retention_note=item.retention_note,
            evidence_refs=list(item.evidence_refs),
            scan_id=scan_id,
            run_id=run_id,
        )
        db.add(row)
        await db.flush()

        if item.finding is not None:
            db.add(PurposeFinding(
                org_id=org_id,
                assessment_id=row.id,
                finding_type=item.finding.finding_type,
                severity=item.finding.severity,
                title=item.finding.title,
                description=item.finding.description,
                # Forced by the finding itself, not by this caller.
                review_required=item.finding.review_required,
            ))
            findings += 1
    await db.flush()
    return {"assessments": len(assessments), "findings": findings}
