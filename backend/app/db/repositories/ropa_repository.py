"""Persistence for Agent 2 (Data Discovery / ROPA).

Versioning rule lives here: `persist_output` never updates an existing ROPA
record in place. A new run inserts version N+1 and marks the previous version
'superseded', so an approved record is never silently rewritten and the full
history stays reconstructable.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.schemas.output import RopaAgentOutput
from app.db.models import (
    RopaDataSource,
    RopaDiscoveryRun,
    RopaFinding,
    RopaRecordRow,
    RopaSchemaBaseline,
    RopaSchemaChange,
)


async def get_data_source(db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID) -> RopaDataSource | None:
    result = await db.execute(
        select(RopaDataSource).where(RopaDataSource.id == source_id, RopaDataSource.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_data_sources(db: AsyncSession, org_id: uuid.UUID) -> list[RopaDataSource]:
    result = await db.execute(
        select(RopaDataSource).where(RopaDataSource.org_id == org_id).order_by(RopaDataSource.created_at.desc())
    )
    return list(result.scalars().all())


async def create_data_source(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    connector: str,
    source_type: str,
    config: dict,
    credential_ref: str | None,
) -> RopaDataSource:
    row = RopaDataSource(
        org_id=org_id, name=name, connector=connector, source_type=source_type,
        config=config, credential_ref=credential_ref,
        credential_rotated_at=datetime.now(UTC) if credential_ref else None,
    )
    db.add(row)
    await db.flush()
    return row


async def mark_source_verified(db: AsyncSession, source: RopaDataSource) -> None:
    source.last_verified_at = datetime.now(UTC)
    await db.flush()


async def find_run_by_idempotency_key(
    db: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> RopaDiscoveryRun | None:
    result = await db.execute(
        select(RopaDiscoveryRun).where(
            RopaDiscoveryRun.org_id == org_id,
            RopaDiscoveryRun.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def create_run(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_name: str,
    data_source_id: uuid.UUID | None = None,
    ingest_mode: str = "connector",
    requested_by_user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> RopaDiscoveryRun:
    row = RopaDiscoveryRun(
        org_id=org_id, source_name=source_name, data_source_id=data_source_id,
        ingest_mode=ingest_mode, status="pending",
        requested_by_user_id=requested_by_user_id, idempotency_key=idempotency_key,
        started_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def list_runs(db: AsyncSession, org_id: uuid.UUID, limit: int = 50) -> list[RopaDiscoveryRun]:
    """Most recent runs for this org, for the dashboard's run list."""
    result = await db.execute(
        select(RopaDiscoveryRun)
        .where(RopaDiscoveryRun.org_id == org_id)
        .order_by(RopaDiscoveryRun.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_run(db: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> RopaDiscoveryRun | None:
    result = await db.execute(
        select(RopaDiscoveryRun).where(RopaDiscoveryRun.id == run_id, RopaDiscoveryRun.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def fail_run(db: AsyncSession, run: RopaDiscoveryRun, error: str) -> None:
    run.status = "failed"
    run.error = error
    run.completed_at = datetime.now(UTC)
    await db.flush()


async def persist_output(
    db: AsyncSession,
    run: RopaDiscoveryRun,
    output: RopaAgentOutput,
    *,
    schema_snapshot: dict | None = None,
) -> tuple[list[RopaRecordRow], list[RopaFinding]]:
    """Write the analysis result, superseding any prior approved/draft version of
    the same processing activity for this org."""
    summary = output.discovery_summary
    run.tables_scanned = summary.tables_scanned
    run.columns_scanned = summary.columns_scanned
    run.personal_data_elements = summary.personal_data_elements_found
    run.overall_confidence = output.confidence_summary.overall_confidence
    # Counts and evidence references only -- never raw values (see 0007's comment
    # on this column).
    run.summary = {
        "sources_scanned": summary.sources_scanned,
        "processing_activities": len(output.processing_activities),
        "risk_findings": len(output.risk_and_gap_findings),
        "human_review_items": len(output.human_review_items),
        "evidence_reference_count": len(output.evidence_references),
        "detected_changes": len(output.change_detection),
        # Stored so this run can later be PROMOTED to the comparison baseline
        # without re-reading the source. Metadata fingerprint only -- table
        # names, column types and resolved classifications, never row values.
        "schema_snapshot": schema_snapshot,
    }
    run.status = "completed"
    run.completed_at = datetime.now(UTC)

    records: list[RopaRecordRow] = []
    for record in output.ropa_records:
        previous = await _latest_version(db, run.org_id, record.processing_activity)
        version = (previous.version + 1) if previous else 1

        row = RopaRecordRow(
            org_id=run.org_id,
            discovery_run_id=run.id,
            processing_activity=record.processing_activity,
            version=version,
            status="in_review" if record.review_required else "draft",
            payload=record.model_dump(mode="json"),
            confidence=record.confidence,
            review_required=record.review_required,
            supersedes_id=previous.id if previous else None,
        )
        db.add(row)
        records.append(row)

        if previous is not None:
            await db.execute(
                update(RopaRecordRow)
                .where(RopaRecordRow.id == previous.id)
                .values(status="superseded", updated_at=datetime.now(UTC))
            )

    findings: list[RopaFinding] = []
    for finding in output.risk_and_gap_findings:
        row = RopaFinding(
            org_id=run.org_id,
            discovery_run_id=run.id,
            finding=finding.finding,
            gap_status=finding.status,
            severity=finding.severity,
            severity_factors=finding.severity_factors,
            related_evidence=finding.related_evidence,
            confidence=finding.confidence,
            recommendation=finding.recommendation,
        )
        db.add(row)
        findings.append(row)

    await db.flush()
    return records, findings


async def _latest_version(db: AsyncSession, org_id: uuid.UUID, activity: str) -> RopaRecordRow | None:
    result = await db.execute(
        select(RopaRecordRow)
        .where(
            RopaRecordRow.org_id == org_id,
            RopaRecordRow.processing_activity == activity,
            RopaRecordRow.status != "superseded",
        )
        .order_by(RopaRecordRow.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_record(db: AsyncSession, record_id: uuid.UUID, org_id: uuid.UUID) -> RopaRecordRow | None:
    result = await db.execute(
        select(RopaRecordRow).where(RopaRecordRow.id == record_id, RopaRecordRow.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_records_for_run(db: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> list[RopaRecordRow]:
    result = await db.execute(
        select(RopaRecordRow)
        .where(RopaRecordRow.discovery_run_id == run_id, RopaRecordRow.org_id == org_id)
        .order_by(RopaRecordRow.processing_activity)
    )
    return list(result.scalars().all())


async def list_findings_for_run(db: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> list[RopaFinding]:
    result = await db.execute(
        select(RopaFinding)
        .where(RopaFinding.discovery_run_id == run_id, RopaFinding.org_id == org_id)
        .order_by(RopaFinding.severity)
    )
    return list(result.scalars().all())


async def get_current_baseline_snapshot(
    db: AsyncSession, org_id: uuid.UUID, source_name: str
) -> dict | None:
    """The promoted schema fingerprint this source's next run is compared against.
    None means no baseline yet -- change detection is then skipped rather than
    reporting a first run as all-new."""
    result = await db.execute(
        select(RopaSchemaBaseline).where(
            RopaSchemaBaseline.org_id == org_id,
            RopaSchemaBaseline.source_name == source_name,
            RopaSchemaBaseline.is_current.is_(True),
        )
    )
    row = result.scalar_one_or_none()
    return row.schema_snapshot if row else None


async def promote_baseline(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_name: str,
    run_id: uuid.UUID,
    snapshot: dict,
    user_id: uuid.UUID | None,
) -> RopaSchemaBaseline:
    """Make this run's schema the new reference point.

    Only ever called explicitly by a human decision -- never automatically after
    a run, matching how scan_schedules.baseline_scan_id behaves for Agent 1. The
    point of a baseline is that someone SAW what changed before it became normal.
    """
    await db.execute(
        update(RopaSchemaBaseline)
        .where(
            RopaSchemaBaseline.org_id == org_id,
            RopaSchemaBaseline.source_name == source_name,
            RopaSchemaBaseline.is_current.is_(True),
        )
        .values(is_current=False)
    )
    row = RopaSchemaBaseline(
        org_id=org_id, source_name=source_name, discovery_run_id=run_id,
        schema_snapshot=snapshot, is_current=True, promoted_by_user_id=user_id,
    )
    db.add(row)
    await db.flush()
    return row


async def persist_changes(
    db: AsyncSession,
    run: RopaDiscoveryRun,
    changes: list,
    *,
    source_name: str,
) -> list[RopaSchemaChange]:
    """Persist detected schema changes so 'what changed and when' is part of the
    permanent record, not a live-only view."""
    rows: list[RopaSchemaChange] = []
    for change in changes:
        row = RopaSchemaChange(
            org_id=run.org_id, source_name=source_name, discovery_run_id=run.id,
            change_type=change.change_type, target=change.target,
            previous_value=change.previous_value, current_value=change.current_value,
            is_material=change.review_required, review_required=change.review_required,
        )
        db.add(row)
        rows.append(row)
    if rows:
        await db.flush()
    return rows


async def list_changes_for_run(
    db: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID
) -> list[RopaSchemaChange]:
    result = await db.execute(
        select(RopaSchemaChange)
        .where(RopaSchemaChange.discovery_run_id == run_id, RopaSchemaChange.org_id == org_id)
        .order_by(RopaSchemaChange.change_type)
    )
    return list(result.scalars().all())


async def get_finding(db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID) -> RopaFinding | None:
    result = await db.execute(
        select(RopaFinding).where(RopaFinding.id == finding_id, RopaFinding.org_id == org_id)
    )
    return result.scalar_one_or_none()
