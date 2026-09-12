"""Controlled execution of an approved DSR action (prompt §23, §24, §25).

This is the only module in Agent 3 that changes data in a customer's system. Its
whole shape is the eight-step revalidation §24 asks for, done at the moment of the
write rather than trusted from when the plan was approved:

    1. tenant        -- the action belongs to this org and this case
    2. case state    -- the case is in a status where execution is legal
    3. identity      -- the requester's verification still stands
    4. approval      -- a CURRENT, unexpired approval exists for THIS action
    5. constraints   -- re-evaluated now, not reused from plan time
    6. authorization -- the source still permits execution, with a write credential
    7. idempotency   -- the key is claimed, atomically, before anything happens
    8. target        -- the record reference is the one the evidence recorded

Only then does a connector open a write connection.

WHAT "DONE" MEANS HERE
----------------------
Three separate facts, never collapsed (§23):

    executed  -- the write was issued and the database reported a result
    verified  -- a fresh read-back confirmed the intended end state
    completed -- both of the above, recorded

An action is only ever reported successful when the read-back agreed. A connector
that reports success without verification raises, and the execution row keeps
`verification_status='failed'` rather than being quietly marked done.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.connectors import factory
from app.agents.dsr.errors import (
    ActionBlockedError,
    ActionFailedError,
    ApprovalRequiredError,
    DsrError,
    DsrNotFoundError,
    SourceNotAuthorizedError,
    VerificationFailedError,
)
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, identity_service
from app.db.models import DsrAction, DsrExecution, DsrRequest
from app.db.repositories import dsr_repository, ropa_repository
from app.services import audit_service

logger = logging.getLogger(__name__)

# Case statuses from which an action may be executed. Enforced here as well as in
# the lifecycle table: this is the one place where being wrong changes customer data.
EXECUTABLE_CASE_STATUSES = frozenset({case.APPROVED, case.EXECUTING})


def idempotency_key(action: DsrAction) -> str:
    """A key derived from WHAT is being done, not from when it was asked for.

    Two requests to perform the same operation on the same record with the same
    payload produce the same key and therefore the same execution row -- which is
    what makes a double-click, an API retry and a worker restart all safe (§25).
    Deriving it from the action id alone would be enough for retries but would not
    survive a re-planned action; including the operation and payload means a
    genuinely different action gets a genuinely different key.
    """
    material = "|".join([
        str(action.id),
        action.operation,
        action.table_name,
        repr(sorted((action.record_reference or {}).items())),
        repr(sorted((action.operation_payload or {}).items())),
    ])
    return hashlib.sha256(material.encode()).hexdigest()


async def execute_action(
    db: AsyncSession,
    request: DsrRequest,
    action_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
    retention_rules: tuple[rules.RetentionRule, ...] = (),
    now: datetime | None = None,
) -> DsrExecution:
    """Execute ONE approved action, or return the already-recorded outcome.

    Never performs an action twice. Never reports a success the read-back did not
    confirm. Never raises without leaving the reason on the execution row.
    """
    moment = now or datetime.now(UTC)

    # ── 1. Tenant ────────────────────────────────────────────────────────────────
    action = await dsr_repository.get_action(db, action_id, request.org_id)
    if action is None or action.request_id != request.id:
        raise DsrNotFoundError(f"action {action_id} not found on case {request.reference}")

    # ── 2. Case state ────────────────────────────────────────────────────────────
    if request.status not in EXECUTABLE_CASE_STATUSES:
        raise ApprovalRequiredError(
            f"case {request.reference} is {request.status}; execution is only permitted "
            f"from {sorted(EXECUTABLE_CASE_STATUSES)}"
        )

    if action.status == "blocked":
        raise ActionBlockedError(
            f"action {action_id} is blocked: {action.blocked_reason}"
        )

    # ── 3. Identity still stands ─────────────────────────────────────────────────
    # Re-checked at write time, not reused from search time: a verification can be
    # revoked or expire between the search and the execution.
    await identity_service.assert_identity_satisfied(db, request)

    # ── 4. A current approval for THIS action ────────────────────────────────────
    if action.requires_approval:
        approval = await dsr_repository.latest_approval_for_action(db, action.id, request.org_id)
        if not approval_service.is_approval_current(approval, now=moment):
            raise ApprovalRequiredError(
                f"action {action_id} has no current approval "
                f"(latest: {approval.decision if approval else 'none'}); "
                "execution is refused"
            )

    # ── 7 (early). Idempotency, claimed before any side effect ───────────────────
    # Deliberately before the connector is built and before any constraint work that
    # could be slow: two concurrent callers must race on the unique index, not on
    # who finishes their pre-flight checks first.
    key = idempotency_key(action)
    execution, is_new = await dsr_repository.claim_execution(
        db,
        org_id=request.org_id,
        request_id=request.id,
        action_id=action.id,
        idempotency_key=key,
        executed_by_user_id=actor_user_id,
        job_id=job_id,
        correlation_id=correlation_id,
    )
    if not is_new:
        # Somebody already claimed this. Return THEIR outcome; do not redo the work.
        logger.info(
            "DSR execution %s already claimed for action %s (status=%s) -- returning the "
            "recorded result rather than executing again",
            execution.id, action.id, execution.status,
        )
        return execution

    execution.status = "running"
    execution.attempts += 1
    execution.started_at = moment
    await db.flush()

    try:
        # ── 5 + 6. Authorization and constraints, re-evaluated NOW ───────────────
        authorization = await dsr_repository.get_source_authorization(
            db, action.data_source_id, request.org_id
        ) if action.data_source_id else None
        data_source = await ropa_repository.get_data_source(
            db, action.data_source_id, request.org_id
        ) if action.data_source_id else None

        if data_source is None or not data_source.enabled:
            raise SourceNotAuthorizedError(
                f"source for action {action_id} is missing or disabled; execution refused"
            )

        connector = factory.build_connector(
            data_source=data_source, authorization=authorization, for_execution=True,
        )
        grant = factory.build_grant(authorization, source_name=data_source.name)

        evidence = (
            await dsr_repository.get_evidence(db, action.evidence_id, request.org_id)
            if action.evidence_id else None
        )
        evaluated = rules.evaluate(
            operation=action.operation,
            table_name=action.table_name,
            grant=grant,
            payload=action.operation_payload,
            record_snapshot=evidence.record_snapshot if evidence else None,
            retention_rules=retention_rules,
            now=moment,
        )
        if rules.verdict(evaluated) == rules.EFFECT_BLOCK:
            raise ActionBlockedError(
                "a constraint that did not apply at plan time now blocks this action: "
                + (rules.blocking_reason(evaluated) or "unspecified")
            )

        # ── 8. Target ────────────────────────────────────────────────────────────
        if evidence is not None and evidence.record_reference != action.record_reference:
            raise ActionBlockedError(
                f"action {action_id} targets a record reference that no longer matches "
                "its evidence; re-run the search before executing"
            )
        if not action.record_reference:
            raise ActionFailedError(f"action {action_id} has no record reference to target")

        action.status = "executing"
        await db.flush()

        outcome = await connector.execute_action(
            table_name=action.table_name,
            record_reference=action.record_reference,
            operation=action.operation,
            payload=action.operation_payload or {},
        )

    except VerificationFailedError as exc:
        # The write may have happened; the read-back did not confirm it. This is the
        # one failure that must never read as either a clean success or a clean
        # no-op, so it gets its own terminal state.
        _fail(execution, action, exc, moment, verification_status="failed")
        await db.flush()
        await _audit_failure(db, request, action, execution, exc)
        raise
    except DsrError as exc:
        _fail(execution, action, exc, moment, verification_status="not_applicable")
        await db.flush()
        await _audit_failure(db, request, action, execution, exc)
        raise
    except Exception as exc:
        # Unexpected. Recorded with the same explicitness -- an execution row left at
        # "running" forever is exactly the silent loss §47 forbids.
        execution.status = "failed"
        execution.error_code = case.ERR_ACTION_FAILED
        execution.error_detail = f"{type(exc).__name__} during execution"
        execution.verification_status = "not_applicable"
        execution.completed_at = moment
        action.status = "failed"
        await db.flush()
        logger.exception("Unexpected error executing DSR action %s", action.id)
        raise ActionFailedError(
            f"action {action_id} failed unexpectedly; see execution {execution.id}"
        ) from exc

    # ── Success means executed AND verified, recorded separately ─────────────────
    execution.rows_affected = outcome.rows_affected
    execution.connector_response = outcome.raw_response
    execution.verification_detail = outcome.verification_detail
    execution.verified_at = moment if outcome.verified else None
    execution.verification_status = "passed" if outcome.verified else "failed"
    execution.status = "verified" if outcome.verified else "failed"
    execution.completed_at = moment
    action.status = "executed" if outcome.verified else "failed"
    await db.flush()

    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=actor_user_id,
        action=case.AUDIT_ACTION_VERIFIED if outcome.verified else case.AUDIT_ACTION_EXECUTED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        before={"action_id": str(action.id), "status": "executing"},
        after={
            "action_id": str(action.id),
            "execution_id": str(execution.id),
            "operation": action.operation,
            "table": action.table_name,
            "rows_affected": outcome.rows_affected,
            "verified": outcome.verified,
        },
    )
    return execution


def _fail(
    execution: DsrExecution,
    action: DsrAction,
    exc: DsrError,
    moment: datetime,
    *,
    verification_status: str,
) -> None:
    execution.status = "failed"
    execution.error_code = exc.code or case.ERR_ACTION_FAILED
    execution.error_detail = exc.message
    execution.verification_status = verification_status
    execution.completed_at = moment
    action.status = "failed"


async def _audit_failure(
    db: AsyncSession, request: DsrRequest, action: DsrAction, execution: DsrExecution, exc: DsrError
) -> None:
    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=execution.executed_by_user_id,
        action=case.AUDIT_FAILED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        after={
            "action_id": str(action.id),
            "execution_id": str(execution.id),
            "error_code": execution.error_code,
            "error_detail": exc.message,
        },
    )
