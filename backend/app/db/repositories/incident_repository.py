"""Persistence for Agent 4 (Breach Response).

Same two rules as the DSR repository, for the same reasons:

  * EVERY read takes an org_id and filters on it. This backend connects to Postgres
    directly, so the RLS policies in 0015 do not apply to it -- that filter IS the
    tenant isolation. A lookup by id alone is an IDOR, so no function here offers one.
    Incident evidence can contain account names, attack paths and occasionally
    credentials, which makes the boundary matter more here than anywhere else.

  * `claim_execution` is the idempotency primitive, and relies on the unique index
    rather than a read-then-write so two concurrent callers cannot both win.

The one addition over the DSR repository: evidence and approvals are append-only at
the database level, so there is deliberately no update path for either. Superseding
evidence means adding a row that points at the old one.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    IncidentAction,
    IncidentAffectedData,
    IncidentAffectedSubjects,
    IncidentAffectedSystem,
    IncidentApproval,
    IncidentCase,
    IncidentCommunication,
    IncidentEvidence,
    IncidentExecution,
    IncidentReport,
    IncidentRiskAssessment,
    IncidentTimelineEntry,
)

# ── Incidents ────────────────────────────────────────────────────────────────────


async def create_incident(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    reference: str,
    title: str,
    description: str,
    source: str,
    detected_at: datetime,
    reported_by: str | None = None,
    occurred_at: datetime | None = None,
    initial_severity: str | None = None,
    due_at: datetime | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> IncidentCase:
    row = IncidentCase(
        org_id=org_id, reference=reference, title=title, description=description,
        source=source, detected_at=detected_at, reported_by=reported_by,
        occurred_at=occurred_at, initial_severity=initial_severity, due_at=due_at,
        correlation_id=correlation_id, idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
    )
    db.add(row)
    await db.flush()
    return row


async def get_incident(db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID) -> IncidentCase | None:
    result = await db.execute(
        select(IncidentCase).where(IncidentCase.id == incident_id, IncidentCase.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def find_by_idempotency_key(
    db: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> IncidentCase | None:
    result = await db.execute(
        select(IncidentCase).where(
            IncidentCase.org_id == org_id, IncidentCase.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()


async def reference_exists(db: AsyncSession, org_id: uuid.UUID, reference: str) -> bool:
    result = await db.execute(
        select(IncidentCase.id)
        .where(IncidentCase.org_id == org_id, IncidentCase.reference == reference)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def list_incidents(
    db: AsyncSession, org_id: uuid.UUID, *, status: str | None = None, limit: int = 50
) -> list[IncidentCase]:
    stmt = select(IncidentCase).where(IncidentCase.org_id == org_id)
    if status:
        stmt = stmt.where(IncidentCase.status == status)
    result = await db.execute(stmt.order_by(IncidentCase.detected_at.desc()).limit(limit))
    return list(result.scalars().all())


async def list_overdue_incidents(db: AsyncSession, *, limit: int = 200) -> list[IncidentCase]:
    """SLA sweep support. Deliberately NOT org-scoped: called by the worker, which acts
    for every tenant rather than on behalf of a signed-in user. The only caller is
    app/agents/breach/services/sla_service.py -- never a request handler.

    FOR UPDATE SKIP LOCKED for the same reason as the DSR sweep's own overdue query:
    two workers sweeping at once would otherwise both flag the same case and both
    append to an append-only audit log. See dsr_repository.list_overdue_requests."""
    result = await db.execute(
        select(IncidentCase)
        .where(
            IncidentCase.due_at.is_not(None),
            IncidentCase.due_at < datetime.now(UTC),
            IncidentCase.sla_breached.is_(False),
            IncidentCase.status.not_in(("closed", "rejected", "cancelled", "partially_completed")),
        )
        .order_by(IncidentCase.due_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


# ── Evidence (append-only: no update path exists, deliberately) ──────────────────


async def add_evidence(db: AsyncSession, org_id: uuid.UUID, row: IncidentEvidence) -> IncidentEvidence:
    """`org_id` is an ASSERTION, not a filter. The row is built by the service from the
    incident it is working on, so it should already carry the right tenant; checking
    anyway means a future caller that builds one from the wrong incident fails loudly
    rather than filing another organisation's evidence."""
    if row.org_id != org_id:
        raise ValueError(
            f"refusing to file evidence for org {row.org_id} against an incident in org {org_id}"
        )
    db.add(row)
    await db.flush()
    return row


async def list_evidence(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentEvidence]:
    result = await db.execute(
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == incident_id, IncidentEvidence.org_id == org_id)
        .order_by(IncidentEvidence.observed_at.nulls_last(), IncidentEvidence.created_at)
    )
    return list(result.scalars().all())


async def get_evidence(
    db: AsyncSession, evidence_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentEvidence | None:
    result = await db.execute(
        select(IncidentEvidence).where(
            IncidentEvidence.id == evidence_id, IncidentEvidence.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


# ── Timeline ─────────────────────────────────────────────────────────────────────


async def add_timeline_entries(
    db: AsyncSession, org_id: uuid.UUID, rows: list[IncidentTimelineEntry]
) -> None:
    for row in rows:
        if row.org_id != org_id:
            raise ValueError("refusing to write a timeline entry into another org's incident")
        db.add(row)
    await db.flush()


async def list_timeline(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentTimelineEntry]:
    result = await db.execute(
        select(IncidentTimelineEntry)
        .where(
            IncidentTimelineEntry.incident_id == incident_id,
            IncidentTimelineEntry.org_id == org_id,
        )
        .order_by(IncidentTimelineEntry.occurred_at)
    )
    return list(result.scalars().all())


# ── Affected systems, data, subjects ─────────────────────────────────────────────


async def add_affected_system(
    db: AsyncSession, row: IncidentAffectedSystem
) -> IncidentAffectedSystem:
    db.add(row)
    await db.flush()
    return row


async def list_affected_systems(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentAffectedSystem]:
    result = await db.execute(
        select(IncidentAffectedSystem)
        .where(
            IncidentAffectedSystem.incident_id == incident_id,
            IncidentAffectedSystem.org_id == org_id,
        )
        .order_by(IncidentAffectedSystem.system_name)
    )
    return list(result.scalars().all())


async def replace_affected_data(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID,
    rows: list[IncidentAffectedData],
) -> None:
    """Re-deriving the affected-data map replaces the derived rows wholesale.

    Rows a human entered (`derived_from='manual'`) are KEPT: a person who recorded
    that they saw health data in the export has said something the ROPA map cannot
    know, and re-running discovery must not erase it.
    """
    existing = await db.execute(
        select(IncidentAffectedData).where(
            IncidentAffectedData.incident_id == incident_id,
            IncidentAffectedData.org_id == org_id,
            IncidentAffectedData.derived_from != "manual",
        )
    )
    for row in existing.scalars().all():
        await db.delete(row)
    for row in rows:
        db.add(row)
    await db.flush()


async def list_affected_data(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentAffectedData]:
    result = await db.execute(
        select(IncidentAffectedData)
        .where(
            IncidentAffectedData.incident_id == incident_id,
            IncidentAffectedData.org_id == org_id,
        )
        .order_by(IncidentAffectedData.data_category)
    )
    return list(result.scalars().all())


async def upsert_affected_subjects(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    subject_group: str,
    record_count: int | None,
    count_basis: str,
    basis_note: str | None,
    confidence: str,
    evidence_id: uuid.UUID | None = None,
) -> IncidentAffectedSubjects:
    result = await db.execute(
        select(IncidentAffectedSubjects).where(
            IncidentAffectedSubjects.incident_id == incident_id,
            IncidentAffectedSubjects.org_id == org_id,
            IncidentAffectedSubjects.subject_group == subject_group,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = IncidentAffectedSubjects(
            org_id=org_id, incident_id=incident_id, subject_group=subject_group
        )
        db.add(row)
    row.record_count = record_count
    row.count_basis = count_basis
    row.basis_note = basis_note
    row.confidence = confidence
    row.evidence_id = evidence_id
    await db.flush()
    return row


async def list_affected_subjects(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentAffectedSubjects]:
    result = await db.execute(
        select(IncidentAffectedSubjects)
        .where(
            IncidentAffectedSubjects.incident_id == incident_id,
            IncidentAffectedSubjects.org_id == org_id,
        )
        .order_by(IncidentAffectedSubjects.subject_group)
    )
    return list(result.scalars().all())


# ── Risk assessments ─────────────────────────────────────────────────────────────


async def next_risk_version(db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(IncidentRiskAssessment.version), 0)).where(
            IncidentRiskAssessment.incident_id == incident_id,
            IncidentRiskAssessment.org_id == org_id,
        )
    )
    return int(result.scalar_one()) + 1


async def create_risk_assessment(
    db: AsyncSession, row: IncidentRiskAssessment
) -> IncidentRiskAssessment:
    db.add(row)
    await db.flush()
    return row


async def supersede_risk_assessments(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> None:
    """Mark earlier assessments superseded rather than deleting them. A risk level an
    organisation acted on last week is part of the incident's history even once the
    evidence has moved on."""
    result = await db.execute(
        select(IncidentRiskAssessment).where(
            IncidentRiskAssessment.incident_id == incident_id,
            IncidentRiskAssessment.org_id == org_id,
            IncidentRiskAssessment.review_status != "superseded",
        )
    )
    for row in result.scalars().all():
        row.review_status = "superseded"
    await db.flush()


async def get_current_risk(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentRiskAssessment | None:
    result = await db.execute(
        select(IncidentRiskAssessment)
        .where(
            IncidentRiskAssessment.incident_id == incident_id,
            IncidentRiskAssessment.org_id == org_id,
            IncidentRiskAssessment.review_status != "superseded",
        )
        .order_by(IncidentRiskAssessment.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_risk_assessments(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentRiskAssessment]:
    result = await db.execute(
        select(IncidentRiskAssessment)
        .where(
            IncidentRiskAssessment.incident_id == incident_id,
            IncidentRiskAssessment.org_id == org_id,
        )
        .order_by(IncidentRiskAssessment.version)
    )
    return list(result.scalars().all())


# ── Actions ──────────────────────────────────────────────────────────────────────


async def add_actions(db: AsyncSession, org_id: uuid.UUID, rows: list[IncidentAction]) -> None:
    for row in rows:
        if row.org_id != org_id:
            raise ValueError("refusing to write an action into another org's incident")
        db.add(row)
    await db.flush()


async def list_actions(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentAction]:
    result = await db.execute(
        select(IncidentAction)
        .where(IncidentAction.incident_id == incident_id, IncidentAction.org_id == org_id)
        .order_by(IncidentAction.created_at)
    )
    return list(result.scalars().all())


async def get_action(
    db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentAction | None:
    result = await db.execute(
        select(IncidentAction).where(
            IncidentAction.id == action_id, IncidentAction.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


# ── Approvals (append-only) ──────────────────────────────────────────────────────


async def record_approval(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    subject: str,
    decision: str,
    action_id: uuid.UUID | None = None,
    risk_assessment_id: uuid.UUID | None = None,
    communication_id: uuid.UUID | None = None,
    reason: str | None = None,
    edited_payload: dict | None = None,
    expires_at: datetime | None = None,
) -> IncidentApproval:
    row = IncidentApproval(
        org_id=org_id, incident_id=incident_id, reviewer_user_id=reviewer_user_id,
        subject=subject, decision=decision, action_id=action_id,
        risk_assessment_id=risk_assessment_id, communication_id=communication_id,
        reason=reason, edited_payload=edited_payload, expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def latest_approval_for_action(
    db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentApproval | None:
    """The most recent decision on this action. Execution re-reads this immediately
    before acting rather than trusting the action's own status column, which could
    have been set by an earlier, since-revoked decision."""
    result = await db.execute(
        select(IncidentApproval)
        .where(IncidentApproval.action_id == action_id, IncidentApproval.org_id == org_id)
        .order_by(IncidentApproval.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_approval_for_communication(
    db: AsyncSession, communication_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentApproval | None:
    result = await db.execute(
        select(IncidentApproval)
        .where(
            IncidentApproval.communication_id == communication_id,
            IncidentApproval.org_id == org_id,
        )
        .order_by(IncidentApproval.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_approvals(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentApproval]:
    result = await db.execute(
        select(IncidentApproval)
        .where(IncidentApproval.incident_id == incident_id, IncidentApproval.org_id == org_id)
        .order_by(IncidentApproval.created_at)
    )
    return list(result.scalars().all())


# ── Executions -- the idempotency ledger ─────────────────────────────────────────


async def claim_execution(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    action_id: uuid.UUID,
    idempotency_key: str,
    execution_mode: str,
    executed_by_user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[IncidentExecution, bool]:
    """Claim the right to perform an action, returning (execution, is_new).

    `is_new=False` means somebody already claimed this key -- a double-click, a retry,
    or a worker that restarted mid-flight -- and the caller must return the existing
    row's outcome rather than acting again. Disabling an account twice is usually
    harmless; rotating a credential twice can lock out the very people trying to
    respond.

    Insert-and-catch inside a SAVEPOINT rather than check-then-insert: two concurrent
    callers both pass an existence check, but only one can win the unique index, and a
    plain rollback here would discard the whole surrounding transaction.
    """
    row = IncidentExecution(
        org_id=org_id, incident_id=incident_id, action_id=action_id,
        idempotency_key=idempotency_key, execution_mode=execution_mode,
        executed_by_user_id=executed_by_user_id, job_id=job_id,
        correlation_id=correlation_id, status="pending",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await find_execution_by_key(db, org_id, idempotency_key)
        if existing is None:
            raise
        return existing, False
    return row, True


async def find_execution_by_key(
    db: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> IncidentExecution | None:
    result = await db.execute(
        select(IncidentExecution).where(
            IncidentExecution.org_id == org_id,
            IncidentExecution.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def list_executions(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentExecution]:
    result = await db.execute(
        select(IncidentExecution)
        .where(
            IncidentExecution.incident_id == incident_id,
            IncidentExecution.org_id == org_id,
        )
        .order_by(IncidentExecution.created_at)
    )
    return list(result.scalars().all())


# ── Communications ───────────────────────────────────────────────────────────────


async def create_communication(
    db: AsyncSession, row: IncidentCommunication
) -> IncidentCommunication:
    db.add(row)
    await db.flush()
    return row


async def get_communication(
    db: AsyncSession, communication_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentCommunication | None:
    result = await db.execute(
        select(IncidentCommunication).where(
            IncidentCommunication.id == communication_id,
            IncidentCommunication.org_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def list_communications(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> list[IncidentCommunication]:
    result = await db.execute(
        select(IncidentCommunication)
        .where(
            IncidentCommunication.incident_id == incident_id,
            IncidentCommunication.org_id == org_id,
        )
        .order_by(IncidentCommunication.created_at)
    )
    return list(result.scalars().all())


# ── Reports ──────────────────────────────────────────────────────────────────────


async def next_report_version(db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(IncidentReport.version), 0)).where(
            IncidentReport.incident_id == incident_id, IncidentReport.org_id == org_id
        )
    )
    return int(result.scalar_one()) + 1


async def create_report(db: AsyncSession, row: IncidentReport) -> IncidentReport:
    db.add(row)
    await db.flush()
    return row


async def get_latest_report(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentReport | None:
    result = await db.execute(
        select(IncidentReport)
        .where(IncidentReport.incident_id == incident_id, IncidentReport.org_id == org_id)
        .order_by(IncidentReport.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Incident audit trail ─────────────────────────────────────────────────────────


async def list_incident_audit(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID, *, limit: int = 500
) -> list[AuditLog]:
    """What happened IN CONSIVA, from the shared append-only audit_logs. Distinct from
    `list_timeline`, which is what happened in the world."""
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.entity_type == "incident_case",
            AuditLog.entity_id == incident_id,
        )
        .order_by(AuditLog.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())
