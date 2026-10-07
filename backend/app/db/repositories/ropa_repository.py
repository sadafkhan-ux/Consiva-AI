"""Persistence for Agent 2 (Data Discovery / ROPA).

Versioning rule lives here: `persist_output` never updates an existing ROPA
record in place. A new run inserts version N+1 and marks the previous version
'superseded', so an approved record is never silently rewritten and the full
history stays reconstructable.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.schemas.output import RopaAgentOutput
from app.agents.ropa.schemas.ropa import RopaRecord
from app.db.models import (
    RopaClassification,
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


async def set_source_enabled(db: AsyncSession, source: RopaDataSource, enabled: bool) -> None:
    """Disabling a source must stop future discovery without deleting its
    history -- `run_discovery_for_source` already checks `source.enabled`
    before enqueuing a job; this is the only place that flips it."""
    source.enabled = enabled
    await db.flush()


async def set_source_credential(db: AsyncSession, source: RopaDataSource, ciphertext: str) -> None:
    """Store an encrypted credential on the source row (migration 0031).
    Overwrites any previous ciphertext -- this IS how a stored credential is
    rotated, same as changing credential_ref's environment entry rotates
    that path."""
    source.credential_ciphertext = ciphertext
    source.credential_rotated_at = datetime.now(UTC)
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


async def find_active_run_for_source(
    db: AsyncSession, org_id: uuid.UUID, data_source_id: uuid.UUID
) -> RopaDiscoveryRun | None:
    """A run against this source that has not finished yet.

    'pending' and 'discovering' are the two non-terminal states; 'completed' and
    'failed' are both done and neither should block a fresh attempt -- re-running
    after a failure is exactly what an operator does next.
    """
    result = await db.execute(
        select(RopaDiscoveryRun)
        .where(
            RopaDiscoveryRun.org_id == org_id,
            RopaDiscoveryRun.data_source_id == data_source_id,
            RopaDiscoveryRun.status.in_(("pending", "discovering")),
        )
        .order_by(RopaDiscoveryRun.created_at.desc())
        .limit(1)
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
    callback_url: str | None = None,
    callback_secret_ciphertext: str | None = None,
) -> RopaDiscoveryRun:
    row = RopaDiscoveryRun(
        org_id=org_id, source_name=source_name, data_source_id=data_source_id,
        ingest_mode=ingest_mode, status="pending",
        requested_by_user_id=requested_by_user_id, idempotency_key=idempotency_key,
        agent_callback_url=callback_url, agent_callback_secret_ciphertext=callback_secret_ciphertext,
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


def _content_fingerprint(record: RopaRecord) -> str:
    """A stable hash over what a REVIEWER would actually see change.

    Deliberately excludes `evidence` (local_ids freshly assigned every
    discovery run, even over byte-identical source data), `confidence` (can
    jitter by floating-point rounding across runs with no real change), and
    `generated_at`/`source_run_id`/`version` (run bookkeeping, not content).
    Two runs producing the same fingerprint means nothing a human reviews
    would look any different -- see persist_output's use of this.
    """
    canonical = {
        "purpose": record.purpose,
        "data_subjects": sorted(record.data_subjects),
        "personal_data_categories": sorted(record.personal_data_categories),
        "data_elements": sorted(record.data_elements),
        "source_systems": sorted(record.source_systems),
        "storage_locations": sorted(record.storage_locations),
        "processors": sorted(
            (p.name, p.role or "", p.location or "", p.dpa_status) for p in record.processors
        ),
        "recipients": sorted(record.recipients),
        "data_flows": sorted((s.from_node, s.to_node) for s in record.data_flows),
        "retention": record.retention,
        "access_roles": sorted(record.access_roles),
        "business_owner": record.business_owner,
    }
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    run.status = "completed"
    run.completed_at = datetime.now(UTC)

    # One query for every distinct activity name instead of one per record --
    # persist_output used to call _latest_version (its own SELECT) inside this
    # loop, so a run with N processing activities issued N round trips purely
    # to find each one's previous version.
    activity_names = [record.processing_activity for record in output.ropa_records]
    occurrences_remaining: dict[str, int] = {}
    for name in activity_names:
        occurrences_remaining[name] = occurrences_remaining.get(name, 0) + 1
    latest_by_activity = await _latest_versions_by_activity(
        db, run.org_id, run.source_name, set(activity_names)
    )

    records: list[RopaRecordRow] = []
    unchanged_count = 0
    for record in output.ropa_records:
        name = record.processing_activity
        previous = latest_by_activity.get(name)
        fingerprint = _content_fingerprint(record)
        occurrences_remaining[name] -= 1

        if previous is not None and previous.content_hash == fingerprint:
            # Re-pushed or re-discovered evidence produced the SAME record in
            # substance -- evidence ids differ every run by design (fresh
            # local_ids), but nothing a reviewer would see changed. Minting a
            # new version here would be pure churn: the run that found this is
            # still fully audited (see _persist_and_audit's caller), it just
            # doesn't fork the record's own history.
            records.append(previous)
            unchanged_count += 1
            continue

        version = (previous.version + 1) if previous else 1

        row = RopaRecordRow(
            org_id=run.org_id,
            discovery_run_id=run.id,
            source_name=run.source_name,
            processing_activity=name,
            version=version,
            status="in_review" if record.review_required else "draft",
            payload=record.model_dump(mode="json"),
            confidence=record.confidence,
            review_required=record.review_required,
            supersedes_id=previous.id if previous else None,
            content_hash=fingerprint,
        )
        db.add(row)
        records.append(row)

        if previous is not None:
            await db.execute(
                update(RopaRecordRow)
                .where(RopaRecordRow.id == previous.id)
                .values(status="superseded", updated_at=datetime.now(UTC))
            )

        latest_by_activity[name] = row
        if occurrences_remaining[name] > 0:
            # A later record in this SAME run shares this activity name --
            # only possible via processing_activity_service's "Unclassified
            # processing: {table}" naming, when two tables from different
            # sources/schemas share a display name and neither matches a
            # purpose rule. That later record needs `row.id` (server-generated,
            # unset until flush) as its own supersedes_id, so flush now. This
            # never fires when every activity name in the run is unique, which
            # is the overwhelming common case -- no N+1 reintroduced there.
            await db.flush()

    # Counts and evidence references only -- never raw values (see 0007's comment
    # on this column). Built after the records loop so unchanged_count is real,
    # not a pre-computed placeholder.
    run.summary = {
        "sources_scanned": summary.sources_scanned,
        "processing_activities": len(output.processing_activities),
        "risk_findings": len(output.risk_and_gap_findings),
        "human_review_items": len(output.human_review_items),
        "evidence_reference_count": len(output.evidence_references),
        "detected_changes": len(output.change_detection),
        # How many of this run's records matched their previous version's
        # content fingerprint and therefore did NOT mint a new version --
        # distinguishes "this run re-confirmed N records unchanged" from
        # silent version churn in the audit trail.
        "unchanged_records": unchanged_count,
        # Stored so this run can later be PROMOTED to the comparison baseline
        # without re-reading the source. Metadata fingerprint only -- table
        # names, column types and resolved classifications, never row values.
        "schema_snapshot": schema_snapshot,
    }

    findings: list[RopaFinding] = []
    for finding in output.risk_and_gap_findings:
        row = RopaFinding(
            org_id=run.org_id,
            discovery_run_id=run.id,
            finding=finding.finding,
            category=finding.category,
            gap_status=finding.status,
            severity=finding.severity,
            severity_factors=finding.severity_factors,
            related_evidence=finding.related_evidence,
            confidence=finding.confidence,
            recommendation=finding.recommendation,
        )
        db.add(row)
        findings.append(row)

    # The per-column trail (classification_service.classify_evidence's full,
    # undropped output) -- persisted alongside the grouped records above, not
    # instead of them. Without this, "why was this column classified this
    # way" is computable for the duration of one request and then gone; see
    # migration 0029.
    for element in output.classifications:
        db.add(RopaClassification(
            org_id=run.org_id,
            discovery_run_id=run.id,
            source_name=element.source,
            schema_name=element.schema_name,
            table_name=element.table or "unknown_table",
            column_name=element.column,
            classification=element.classification,
            data_subject=element.data_subject,
            confidence=element.confidence,
            evidence=element.evidence,
            review_required=element.review_required,
            review_reason=element.review_reason,
        ))

    await db.flush()
    return records, findings


async def _latest_versions_by_activity(
    db: AsyncSession, org_id: uuid.UUID, source_name: str, activities: set[str]
) -> dict[str, RopaRecordRow]:
    """The current (non-superseded) version of every given processing
    activity, in one query -- persist_output used to call a single-activity
    version of this (one SELECT per record) inside its loop, so a run with N
    processing activities issued N round trips purely to find each one's
    previous version.

    Scoped to the SOURCE as well as the org (migration 0030): without
    source_name, two different sources producing an identically-named
    activity -- e.g. "Marketing Communications" -- would share one version
    chain, and a run against one source would supersede a record that
    actually belongs to the other.

    If two non-superseded rows ever share a name
    (only possible if two tables with the same schema-qualified name from
    different sources both end up named "Unclassified processing: ..." --
    see processing_activity_service.py), the higher version wins, matching
    what _latest_version's own `.order_by(version.desc()).limit(1)` would
    return for either one individually.
    """
    if not activities:
        return {}
    result = await db.execute(
        select(RopaRecordRow)
        .where(
            RopaRecordRow.org_id == org_id,
            RopaRecordRow.source_name == source_name,
            RopaRecordRow.processing_activity.in_(activities),
            RopaRecordRow.status != "superseded",
        )
        .order_by(RopaRecordRow.processing_activity, RopaRecordRow.version.desc())
    )
    latest: dict[str, RopaRecordRow] = {}
    for row in result.scalars().all():
        latest.setdefault(row.processing_activity, row)
    return latest


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


async def list_classifications_for_run(
    db: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID
) -> list[RopaClassification]:
    result = await db.execute(
        select(RopaClassification)
        .where(RopaClassification.discovery_run_id == run_id, RopaClassification.org_id == org_id)
        .order_by(RopaClassification.table_name, RopaClassification.column_name)
    )
    return list(result.scalars().all())
