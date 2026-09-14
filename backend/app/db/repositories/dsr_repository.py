"""Persistence for Agent 3 (DSR Fulfillment).

Two rules live here rather than in the services, because they are the kind of thing
a future caller would otherwise have to remember:

  * EVERY read takes an org_id and filters on it. This backend connects to Postgres
    directly, so the RLS policies in 0011 do not apply to it -- that filter IS the
    tenant isolation, exactly as finding_repository's join is for Agent 1. A lookup
    by id alone is an IDOR, so no function here offers one.

  * `claim_execution` is the idempotency primitive. It is the only way an execution
    row is created, and it relies on the unique index on (org_id, idempotency_key)
    rather than a read-then-write, so two concurrent callers cannot both win.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    DsrAction,
    DsrActionPlan,
    DsrApproval,
    DsrEvidence,
    DsrExecution,
    DsrIdentityVerification,
    DsrRequest,
    DsrResponse,
    DsrRetentionRule,
    DsrSearchRun,
    DsrSourceAuthorization,
)

# ── Cases ────────────────────────────────────────────────────────────────────────


async def create_request(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    reference: str,
    raw_request: str,
    due_at: datetime,
    requester_email: str | None = None,
    requester_phone: str | None = None,
    requester_reference: str | None = None,
    idempotency_key: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> DsrRequest:
    row = DsrRequest(
        org_id=org_id, reference=reference, raw_request=raw_request, due_at=due_at,
        requester_email=requester_email, requester_phone=requester_phone,
        requester_reference=requester_reference, idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
    )
    db.add(row)
    await db.flush()
    return row


async def get_request(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> DsrRequest | None:
    result = await db.execute(
        select(DsrRequest).where(DsrRequest.id == request_id, DsrRequest.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def find_request_by_idempotency_key(
    db: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> DsrRequest | None:
    result = await db.execute(
        select(DsrRequest).where(
            DsrRequest.org_id == org_id, DsrRequest.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()


async def reference_exists(db: AsyncSession, org_id: uuid.UUID, reference: str) -> bool:
    """Whether this org already has a case with that reference.

    A pre-check, not the guarantee: `unique (org_id, reference)` in 0011 is what
    actually enforces it. This exists so intake can draw a different reference
    instead of surfacing an integrity error to the requester.
    """
    result = await db.execute(
        select(DsrRequest.id).where(
            DsrRequest.org_id == org_id, DsrRequest.reference == reference
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def list_requests(
    db: AsyncSession, org_id: uuid.UUID, *, status: str | None = None, limit: int = 50
) -> list[DsrRequest]:
    stmt = select(DsrRequest).where(DsrRequest.org_id == org_id)
    if status:
        stmt = stmt.where(DsrRequest.status == status)
    result = await db.execute(stmt.order_by(DsrRequest.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def list_overdue_requests(db: AsyncSession, *, limit: int = 200) -> list[DsrRequest]:
    """SLA sweep support. Deliberately NOT org-scoped: this is called by the worker,
    which acts for every tenant, not on behalf of a signed-in user. It is the one
    function here without an org filter, and the only caller is
    app/agents/dsr/services/sla_service.py -- never a request handler.
    """
    result = await db.execute(
        select(DsrRequest)
        .where(
            DsrRequest.due_at < datetime.now(UTC),
            DsrRequest.sla_breached.is_(False),
            DsrRequest.status.not_in(("completed", "rejected", "cancelled", "expired", "partially_completed")),
        )
        .order_by(DsrRequest.due_at)
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Identity verification ────────────────────────────────────────────────────────


async def create_identity_verification(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    method: str,
    challenge_hash: str | None,
    expires_at: datetime | None,
    max_attempts: int = 5,
) -> DsrIdentityVerification:
    row = DsrIdentityVerification(
        org_id=org_id, request_id=request_id, method=method,
        challenge_hash=challenge_hash, expires_at=expires_at, max_attempts=max_attempts,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def get_latest_identity_verification(
    db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID
) -> DsrIdentityVerification | None:
    result = await db.execute(
        select(DsrIdentityVerification)
        .where(
            DsrIdentityVerification.request_id == request_id,
            DsrIdentityVerification.org_id == org_id,
        )
        .order_by(DsrIdentityVerification.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Search runs and evidence ─────────────────────────────────────────────────────


async def create_search_run(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    source_name: str,
    data_source_id: uuid.UUID | None = None,
    identifier_kinds: list | None = None,
    job_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> DsrSearchRun:
    row = DsrSearchRun(
        org_id=org_id, request_id=request_id, source_name=source_name,
        data_source_id=data_source_id, identifier_kinds=identifier_kinds or [],
        job_id=job_id, correlation_id=correlation_id,
        status="pending", started_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def list_search_runs(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> list[DsrSearchRun]:
    result = await db.execute(
        select(DsrSearchRun)
        .where(DsrSearchRun.request_id == request_id, DsrSearchRun.org_id == org_id)
        .order_by(DsrSearchRun.created_at)
    )
    return list(result.scalars().all())


async def add_evidence(db: AsyncSession, org_id: uuid.UUID, rows: list[DsrEvidence]) -> None:
    """Insert evidence rows. `org_id` is not a filter here -- it is an ASSERTION.

    These rows are built by the search service from the case it is working on, so
    they should already carry the right tenant. Checking it anyway means a future
    caller that builds a row from the wrong case fails loudly here rather than
    writing one tenant's evidence into another's case.
    """
    for row in rows:
        if row.org_id != org_id:
            raise ValueError(
                f"refusing to write evidence for org {row.org_id} into a case in org {org_id}"
            )
        db.add(row)
    await db.flush()


async def list_evidence(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> list[DsrEvidence]:
    result = await db.execute(
        select(DsrEvidence)
        .where(DsrEvidence.request_id == request_id, DsrEvidence.org_id == org_id)
        .order_by(DsrEvidence.source_name, DsrEvidence.table_name)
    )
    return list(result.scalars().all())


async def get_evidence(db: AsyncSession, evidence_id: uuid.UUID, org_id: uuid.UUID) -> DsrEvidence | None:
    result = await db.execute(
        select(DsrEvidence).where(DsrEvidence.id == evidence_id, DsrEvidence.org_id == org_id)
    )
    return result.scalar_one_or_none()


# ── Plans and actions ────────────────────────────────────────────────────────────


async def next_plan_version(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(DsrActionPlan.version), 0)).where(
            DsrActionPlan.request_id == request_id, DsrActionPlan.org_id == org_id
        )
    )
    return int(result.scalar_one()) + 1


async def create_plan(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    version: int,
    summary: str,
    constraints_evaluated: list,
    requires_approval: bool,
) -> DsrActionPlan:
    row = DsrActionPlan(
        org_id=org_id, request_id=request_id, version=version, summary=summary,
        constraints_evaluated=constraints_evaluated, requires_approval=requires_approval,
        status="draft",
    )
    db.add(row)
    await db.flush()
    return row


async def get_plan(db: AsyncSession, plan_id: uuid.UUID, org_id: uuid.UUID) -> DsrActionPlan | None:
    result = await db.execute(
        select(DsrActionPlan).where(DsrActionPlan.id == plan_id, DsrActionPlan.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def get_current_plan(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> DsrActionPlan | None:
    """The highest-versioned plan that has not been superseded."""
    result = await db.execute(
        select(DsrActionPlan)
        .where(
            DsrActionPlan.request_id == request_id,
            DsrActionPlan.org_id == org_id,
            DsrActionPlan.status != "superseded",
        )
        .order_by(DsrActionPlan.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def add_actions(db: AsyncSession, org_id: uuid.UUID, rows: list[DsrAction]) -> None:
    """Insert planned actions. Same tenant assertion as `add_evidence`."""
    for row in rows:
        if row.org_id != org_id:
            raise ValueError(
                f"refusing to write an action for org {row.org_id} into a plan in org {org_id}"
            )
        db.add(row)
    await db.flush()


async def list_actions(db: AsyncSession, plan_id: uuid.UUID, org_id: uuid.UUID) -> list[DsrAction]:
    result = await db.execute(
        select(DsrAction)
        .where(DsrAction.plan_id == plan_id, DsrAction.org_id == org_id)
        .order_by(DsrAction.created_at)
    )
    return list(result.scalars().all())


async def get_action(db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID) -> DsrAction | None:
    result = await db.execute(
        select(DsrAction).where(DsrAction.id == action_id, DsrAction.org_id == org_id)
    )
    return result.scalar_one_or_none()


# ── Approvals ────────────────────────────────────────────────────────────────────


async def record_approval(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    decision: str,
    plan_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
    reason: str | None = None,
    edited_payload: dict | None = None,
    expires_at: datetime | None = None,
) -> DsrApproval:
    row = DsrApproval(
        org_id=org_id, request_id=request_id, reviewer_user_id=reviewer_user_id,
        decision=decision, plan_id=plan_id, action_id=action_id, reason=reason,
        edited_payload=edited_payload, expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def latest_approval_for_action(
    db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID
) -> DsrApproval | None:
    """The most recent decision on this action. Execution re-reads this immediately
    before writing (§24) rather than trusting the action's own status column, which
    could have been set by an earlier, since-revoked decision."""
    result = await db.execute(
        select(DsrApproval)
        .where(DsrApproval.action_id == action_id, DsrApproval.org_id == org_id)
        .order_by(DsrApproval.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_approvals(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> list[DsrApproval]:
    result = await db.execute(
        select(DsrApproval)
        .where(DsrApproval.request_id == request_id, DsrApproval.org_id == org_id)
        .order_by(DsrApproval.created_at)
    )
    return list(result.scalars().all())


# ── Executions -- the idempotency ledger ─────────────────────────────────────────


async def claim_execution(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    action_id: uuid.UUID,
    idempotency_key: str,
    executed_by_user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[DsrExecution, bool]:
    """Claim the right to execute an action, returning (execution, is_new).

    `is_new=False` means somebody already claimed this key -- a double-click, a
    retried request, or a worker that restarted mid-flight. The caller must then
    return the EXISTING row's outcome rather than performing the action again (§25).

    Implemented as insert-and-catch rather than check-then-insert on purpose: two
    concurrent callers both pass a prior existence check, but only one can win the
    unique index on (org_id, idempotency_key).

    The insert runs inside a SAVEPOINT. A plain `db.rollback()` here would discard
    the ENTIRE session transaction, and execute_queued_actions runs several actions
    in one session -- so a duplicate key on the third action would silently undo the
    execution rows, case transitions and audit entries already written for the first
    two. The SAVEPOINT confines the rollback to this one failed insert.
    """
    row = DsrExecution(
        org_id=org_id, request_id=request_id, action_id=action_id,
        idempotency_key=idempotency_key, executed_by_user_id=executed_by_user_id,
        job_id=job_id, correlation_id=correlation_id, status="pending",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await find_execution_by_key(db, org_id, idempotency_key)
        if existing is None:
            # The unique index rejected the insert but no row is visible. Something
            # other than the idempotency constraint failed -- do not silently treat
            # that as "already done".
            raise
        return existing, False
    return row, True


async def find_execution_by_key(
    db: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> DsrExecution | None:
    result = await db.execute(
        select(DsrExecution).where(
            DsrExecution.org_id == org_id, DsrExecution.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()


async def list_executions(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> list[DsrExecution]:
    result = await db.execute(
        select(DsrExecution)
        .where(DsrExecution.request_id == request_id, DsrExecution.org_id == org_id)
        .order_by(DsrExecution.created_at)
    )
    return list(result.scalars().all())


# ── Responses ────────────────────────────────────────────────────────────────────


async def create_response(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    version: int,
    body_text: str,
    grounded_facts: list,
    drafted_by_model: str | None = None,
) -> DsrResponse:
    row = DsrResponse(
        org_id=org_id, request_id=request_id, version=version, body_text=body_text,
        grounded_facts=grounded_facts, drafted_by_model=drafted_by_model, status="draft",
    )
    db.add(row)
    await db.flush()
    return row


async def get_latest_response(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> DsrResponse | None:
    result = await db.execute(
        select(DsrResponse)
        .where(DsrResponse.request_id == request_id, DsrResponse.org_id == org_id)
        .order_by(DsrResponse.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def next_response_version(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(DsrResponse.version), 0)).where(
            DsrResponse.request_id == request_id, DsrResponse.org_id == org_id
        )
    )
    return int(result.scalar_one()) + 1


# ── Source authorizations ────────────────────────────────────────────────────────


async def list_source_authorizations(db: AsyncSession, org_id: uuid.UUID) -> list[DsrSourceAuthorization]:
    result = await db.execute(
        select(DsrSourceAuthorization).where(
            DsrSourceAuthorization.org_id == org_id,
            DsrSourceAuthorization.enabled.is_(True),
        )
    )
    return list(result.scalars().all())


async def get_source_authorization(
    db: AsyncSession, data_source_id: uuid.UUID, org_id: uuid.UUID
) -> DsrSourceAuthorization | None:
    result = await db.execute(
        select(DsrSourceAuthorization).where(
            DsrSourceAuthorization.data_source_id == data_source_id,
            DsrSourceAuthorization.org_id == org_id,
        )
    )
    return result.scalar_one_or_none()


# ── Retention rules (organisation configuration) ─────────────────────────────────


async def list_retention_rules(
    db: AsyncSession, org_id: uuid.UUID, *, data_source_id: uuid.UUID | None = None
) -> list[DsrRetentionRule]:
    """Every enabled rule that could apply to this source.

    A rule with a NULL data_source_id is organisation-wide and applies to any source
    holding a table of that name, so both are returned and the caller matches on the
    table. Filtering the wide rules out here would silently drop the policies most
    likely to matter.
    """
    stmt = select(DsrRetentionRule).where(
        DsrRetentionRule.org_id == org_id,
        DsrRetentionRule.enabled.is_(True),
    )
    if data_source_id is not None:
        stmt = stmt.where(
            or_(
                DsrRetentionRule.data_source_id == data_source_id,
                DsrRetentionRule.data_source_id.is_(None),
            )
        )
    result = await db.execute(stmt.order_by(DsrRetentionRule.table_name))
    return list(result.scalars().all())


async def upsert_retention_rule(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    table_name: str,
    date_column: str,
    retention_days: int,
    authority: str,
    data_source_id: uuid.UUID | None = None,
    applies_to_operations: list | None = None,
    notes: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> DsrRetentionRule:
    existing = await db.execute(
        select(DsrRetentionRule).where(
            DsrRetentionRule.org_id == org_id,
            DsrRetentionRule.table_name == table_name,
            DsrRetentionRule.date_column == date_column,
            DsrRetentionRule.data_source_id.is_(None)
            if data_source_id is None
            else DsrRetentionRule.data_source_id == data_source_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = DsrRetentionRule(
            org_id=org_id, data_source_id=data_source_id, table_name=table_name,
            date_column=date_column, created_by_user_id=created_by_user_id,
        )
        db.add(row)
    row.retention_days = retention_days
    row.authority = authority
    row.applies_to_operations = applies_to_operations or ["delete_record"]
    row.notes = notes
    row.enabled = True
    await db.flush()
    return row


async def delete_retention_rule(db: AsyncSession, rule_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    """Disable rather than delete: a rule that blocked an erasure last month is part of
    why that case ended the way it did, and the audit trail references it."""
    result = await db.execute(
        select(DsrRetentionRule).where(
            DsrRetentionRule.id == rule_id, DsrRetentionRule.org_id == org_id
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    row.enabled = False
    await db.flush()
    return True


# ── Source authorizations (write side) ───────────────────────────────────────────


async def upsert_source_authorization(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    data_source_id: uuid.UUID,
    searchable_tables: list,
    identity_tables: list,
    identifier_columns: dict,
    returnable_columns: dict,
    record_key_columns: dict,
    erasable_columns: dict,
    allow_execution: bool,
    write_credential_ref: str | None,
) -> DsrSourceAuthorization:
    """Create or replace a source's DSR authorization.

    Replaces wholesale rather than merging. An allowlist that is partially updated is
    the worst of both: an administrator removing a table would find it still
    searchable, which is precisely the mistake this configuration exists to prevent.
    """
    existing = await get_source_authorization(db, data_source_id, org_id)
    if existing is None:
        existing = DsrSourceAuthorization(org_id=org_id, data_source_id=data_source_id)
        db.add(existing)
    existing.searchable_tables = searchable_tables
    existing.identity_tables = identity_tables
    existing.identifier_columns = identifier_columns
    existing.returnable_columns = returnable_columns
    existing.record_key_columns = record_key_columns
    existing.erasable_columns = erasable_columns
    existing.allow_execution = allow_execution
    existing.write_credential_ref = write_credential_ref
    existing.enabled = True
    await db.flush()
    return existing


# ── Case timeline ────────────────────────────────────────────────────────────────


async def list_case_audit(
    db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID, *, limit: int = 500
) -> list[AuditLog]:
    """The case timeline. There is no dsr_case_events table -- this IS the timeline,
    read from the shared, append-only audit_logs via the (entity_type, entity_id)
    index 0001 already created."""
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.entity_type == "dsr_request",
            AuditLog.entity_id == request_id,
        )
        .order_by(AuditLog.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())
