"""Application-level orchestration for Agent 2: run a discovery, persist the
result, and record human-review decisions.

Reuses Agent 1's infrastructure rather than duplicating it:
  * audit trail  -> db/repositories/audit_repository.record() (already generic
                    over entity_type/entity_id, so no change was needed there)
  * auth/tenancy -> callers pass the org_id from core/security.CurrentUser
  * persistence  -> db/repositories/ropa_repository
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.factory import build_connector
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import RopaAgentOutput
from app.agents.ropa.services import change_detection_service, discovery_service
from app.db.models import RopaDiscoveryRun, RopaFinding, RopaRecordRow
from app.db.repositories import audit_repository, ropa_repository

_DECISIONS = ("approved", "rejected", "edited")


async def run_discovery_for_source(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    user_id: uuid.UUID | None,
    idempotency_key: str | None = None,
) -> RopaDiscoveryRun:
    """CONNECT -> READ -> VALIDATE -> TRANSFORM -> PERSIST for one configured source."""
    if idempotency_key:
        existing = await ropa_repository.find_run_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing

    source = await ropa_repository.get_data_source(db, source_id, org_id)
    if source is None:
        raise ValueError(f"data source {source_id} not found for this organization")
    if not source.enabled:
        raise ValueError(f"data source {source.name!r} is disabled")

    run = await ropa_repository.create_run(
        db, org_id=org_id, source_name=source.name, data_source_id=source.id,
        ingest_mode="connector", requested_by_user_id=user_id, idempotency_key=idempotency_key,
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_discovery.requested",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={"source": source.name, "connector": source.connector},
    )

    try:
        connector = build_connector(
            connector=source.connector, config=source.config, credential_ref=source.credential_ref
        )
        run.status = "discovering"
        await db.flush()

        output = await discovery_service.discover_and_analyze(
            connector, org_id=str(org_id), source_name=source.name
        )
    except (ConnectorError, ValueError) as exc:
        # The message names the credential REF, never a secret value.
        await ropa_repository.fail_run(db, run, f"{type(exc).__name__}: {exc}")
        await audit_repository.record(
            db, org_id=org_id, actor_user_id=user_id, action="ropa_discovery.failed",
            entity_type="ropa_discovery_run", entity_id=run.id, after={"error": str(exc)},
        )
        return run

    await ropa_repository.mark_source_verified(db, source)
    await _persist_and_audit(db, run, output, org_id=org_id, user_id=user_id)
    return run


async def ingest_pushed_evidence(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_name: str,
    evidence: DiscoveryEvidence,
    user_id: uuid.UUID | None,
    idempotency_key: str | None = None,
    integration_key_name: str | None = None,
    correlation_id: str | None = None,
    schema_version: str | None = None,
) -> RopaDiscoveryRun:
    """Analyze evidence collected by an EXTERNAL integration.

    This is the safer integration boundary for a third party like PrepMyEvent:
    their adapter reads their own database and posts structured evidence, so
    Consiva never holds their production credentials at all. The evidence has
    already been validated against the DiscoveryEvidence schema by FastAPI before
    reaching here.
    """
    if idempotency_key:
        existing = await ropa_repository.find_run_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing

    run = await ropa_repository.create_run(
        db, org_id=org_id, source_name=source_name, ingest_mode="evidence_push",
        requested_by_user_id=user_id, idempotency_key=idempotency_key,
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_evidence.ingested",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={
            "source": source_name,
            "tables": len(evidence.tables),
            "columns": len(evidence.columns),
            # Attributes a machine-authenticated run to its key, since there is
            # no user to attribute it to.
            "integration_key": integration_key_name,
            # Threads the sender's id into the audit trail so one push can be
            # traced across the adapter's logs, this API and the run.
            "correlation_id": correlation_id,
            "schema_version": schema_version,
        },
    )

    run.status = "analyzing"
    await db.flush()
    baseline = await ropa_repository.get_current_baseline_snapshot(db, org_id, source_name)
    output = discovery_service.run_pipeline(evidence, baseline_snapshot=baseline)
    await _persist_and_audit(db, run, output, org_id=org_id, user_id=user_id, evidence=evidence)
    await ropa_repository.persist_changes(
        db, run, output.change_detection, source_name=source_name
    )
    return run


async def execute_queued_discovery(run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Worker entry point for a queued connector discovery (job_type
    'ropa_discovery').

    Opens its own session because the worker has no request scope -- same
    pattern as analysis_service.run_analysis.
    """
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        run = await ropa_repository.get_run(db, run_id, org_id)
        if run is None:
            raise ValueError(f"ROPA run {run_id} not found for org {org_id}")
        if run.data_source_id is None:
            raise ValueError(f"ROPA run {run_id} has no data source to connect to")

        source = await ropa_repository.get_data_source(db, run.data_source_id, org_id)
        if source is None:
            raise ValueError(f"data source {run.data_source_id} not found")

        try:
            connector = build_connector(
                connector=source.connector, config=source.config, credential_ref=source.credential_ref
            )
            run.status = "discovering"
            await db.flush()
            baseline = await ropa_repository.get_current_baseline_snapshot(db, org_id, source.name)
            output = await discovery_service.discover_and_analyze(
                connector, org_id=str(org_id), source_name=source.name, baseline_snapshot=baseline
            )
        except (ConnectorError, ValueError) as exc:
            await ropa_repository.fail_run(db, run, f"{type(exc).__name__}: {exc}")
            await audit_repository.record(
                db, org_id=org_id, actor_user_id=None, action="ropa_discovery.failed",
                entity_type="ropa_discovery_run", entity_id=run.id, after={"error": str(exc)},
            )
            await db.commit()
            raise

        await ropa_repository.mark_source_verified(db, source)
        await _persist_and_audit(db, run, output, org_id=org_id, user_id=None)
        await ropa_repository.persist_changes(db, run, output.change_detection, source_name=source.name)
        await db.commit()


async def _persist_and_audit(
    db: AsyncSession,
    run: RopaDiscoveryRun,
    output: RopaAgentOutput,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    evidence: DiscoveryEvidence | None = None,
) -> None:
    snapshot = (
        change_detection_service.build_snapshot(evidence, output.classifications)
        if evidence is not None
        else None
    )
    records, findings = await ropa_repository.persist_output(
        db, run, output, schema_snapshot=snapshot
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_discovery.completed",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={
            "ropa_records": len(records),
            "findings": len(findings),
            "personal_data_elements": run.personal_data_elements,
        },
    )


async def decide_record(
    db: AsyncSession,
    *,
    record_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
) -> RopaRecordRow:
    """Approve / reject / edit a ROPA record -- the same three actions Agent 1's
    review_service exposes for consent findings, now available for ROPA."""
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS}")
    if decision == "edited" and not edited_payload:
        raise ValueError("an edit decision requires edited_payload")

    record = await ropa_repository.get_record(db, record_id, org_id)
    if record is None:
        raise ValueError(f"ROPA record {record_id} not found for this organization")
    if record.status == "superseded":
        raise ValueError("cannot decide on a superseded record; act on the current version")

    before = {"status": record.status, "review_required": record.review_required}
    record.status = "approved" if decision in ("approved", "edited") else "rejected"
    record.review_required = False
    record.decided_by_user_id = user_id
    record.decided_at = datetime.now(UTC)
    record.decision_reason = reason
    if edited_payload:
        record.edited_payload = edited_payload
    record.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action=f"ropa_record.{decision}",
        entity_type="ropa_record", entity_id=record.id,
        before=before, after={"status": record.status, "reason": reason},
    )
    return record


async def decide_finding(
    db: AsyncSession,
    *,
    finding_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
) -> RopaFinding:
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS}")
    if decision == "edited" and not edited_payload:
        raise ValueError("an edit decision requires edited_payload")

    finding = await ropa_repository.get_finding(db, finding_id, org_id)
    if finding is None:
        raise ValueError(f"ROPA finding {finding_id} not found for this organization")

    before = {"review_status": finding.review_status}
    finding.review_status = decision
    finding.decided_by_user_id = user_id
    finding.decided_at = datetime.now(UTC)
    finding.decision_reason = reason
    if edited_payload:
        finding.edited_payload = edited_payload
    await db.flush()

    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action=f"ropa_finding.{decision}",
        entity_type="ropa_finding", entity_id=finding.id,
        before=before, after={"review_status": decision, "reason": reason},
    )
    return finding
