"""Agent 3 (DSR Fulfillment) API.

Every route is authenticated and org-scoped through the SAME dependency Agent 1 and
Agent 2 use (core/security.get_current_user), so tenancy behaves identically across
all three agents. Nothing here accepts an org_id from the caller -- it comes from
the verified token and nowhere else, which is what closes the IDOR §37 asks about.

The long-running steps (search, execution) are QUEUED on the existing agent_jobs
queue rather than run inside the request. A DSR search takes as long as the slowest
authorized source; an execution must survive a dropped connection. Both return the
case with its new status, and the UI polls -- it never shows progress the backend
did not report (§33).
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.errors import CaseNotReadyError
from app.agents.dsr.schemas import case as case_vocab
from app.agents.dsr.services import (
    approval_service,
    case_service,
    identity_service,
    lifecycle,
    response_service,
    sla_service,
)
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import dsr_repository
from app.db.session import get_db
from app.jobs import queue
from app.services import audit_service, dsr_run_service

router = APIRouter(prefix="/api/v1/dsr", tags=["dsr"])


# ── Request models ───────────────────────────────────────────────────────────────

class DsrCreate(BaseModel):
    raw_request: str = Field(min_length=1, max_length=5000)
    requester_email: str | None = Field(default=None, max_length=320)
    requester_phone: str | None = Field(default=None, max_length=40)
    requester_reference: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("requester_email")
    @classmethod
    def _looks_like_email(cls, value: str | None) -> str | None:
        if value and "@" not in value:
            raise ValueError("requester_email must contain '@'")
        return value


class VerifyIdentity(BaseModel):
    """Either answer a challenge, or record an out-of-band verification."""

    challenge: str | None = Field(default=None, max_length=200)
    manual: bool = False
    evidence_note: str | None = Field(default=None, max_length=1000)


class ClassifyRequest(BaseModel):
    override_type: str | None = Field(default=None, max_length=40)


class Decision(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=2000)
    edited_payload: dict | None = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in approval_service.DECISIONS:
            raise ValueError(f"decision must be one of {sorted(approval_service.DECISIONS)}")
        return value


# ── Response shaping ─────────────────────────────────────────────────────────────

def _case_response(request, *, identity_status: str | None = None) -> dict:
    """The case as the UI sees it. `allowed_transitions` comes from the same state
    machine the backend enforces, so the UI can grey out what is genuinely
    impossible rather than maintaining its own copy of the rules (§33, §34)."""
    return {
        "id": str(request.id),
        "reference": request.reference,
        "status": request.status,
        "request_type": request.request_type,
        "classification_method": request.classification_method,
        "classification_confidence": request.classification_confidence,
        "raw_request": request.raw_request,
        "requester_email": request.requester_email,
        "requester_phone": request.requester_phone,
        "requester_reference": request.requester_reference,
        "error_code": request.error_code,
        "error_detail": request.error_detail,
        "identity_status": identity_status,
        "allowed_transitions": sorted(lifecycle.allowed_transitions(request.status)),
        "is_terminal": lifecycle.is_terminal(request.status),
        "sla": sla_service.sla_view(request),
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "closed_at": request.closed_at.isoformat() if request.closed_at else None,
    }


async def _case_with_identity(db: AsyncSession, request) -> dict:
    verification = await dsr_repository.get_latest_identity_verification(
        db, request.id, request.org_id
    )
    return _case_response(request, identity_status=identity_service.effective_status(verification))


# ── Cases ────────────────────────────────────────────────────────────────────────

@router.post("/requests", status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: DsrCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request, is_new = await case_service.create_case(
        db,
        org_id=uuid.UUID(user.org_id),
        raw_request=payload.raw_request,
        requester_email=payload.requester_email,
        requester_phone=payload.requester_phone,
        requester_reference=payload.requester_reference,
        idempotency_key=payload.idempotency_key,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    if is_new:
        # Classify immediately: it reads only the requester's own words, so it is not
        # behind the identity gate, and a classified case is one a reviewer can triage.
        await case_service.classify_case(db, request, actor_user_id=uuid.UUID(user.user_id))
    await db.commit()
    return await _case_with_identity(db, request)


@router.get("/requests")
async def list_requests(
    status_filter: str | None = None,
    limit: int = 50,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await dsr_repository.list_requests(
        db, uuid.UUID(user.org_id), status=status_filter, limit=min(limit, 200)
    )
    return [_case_response(r) for r in rows]


@router.get("/requests/{request_id}")
async def get_request(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    return await _case_with_identity(db, request)


@router.post("/requests/{request_id}/classify")
async def classify(
    request_id: uuid.UUID,
    payload: ClassifyRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    await case_service.classify_case(
        db, request, actor_user_id=uuid.UUID(user.user_id), override_type=payload.override_type
    )
    await db.commit()
    return await _case_with_identity(db, request)


# ── Identity verification ────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/identity/challenge")
async def start_identity_challenge(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue an email challenge.

    The plaintext challenge is returned ONCE, to the authenticated operator, for
    delivery to the requester -- no outbound email provider is configured in this
    project, so pretending it was sent would be a lie. It is stored only as a hash
    and cannot be recovered afterwards.
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    verification, challenge = await identity_service.start_challenge(db, request)
    if request.status == case_vocab.RECEIVED:
        await case_service.transition(
            db, request, case_vocab.IDENTITY_PENDING,
            actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_IDENTITY_STARTED,
        )
    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_IDENTITY_STARTED, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id,
        after={"method": "email_challenge", "expires_at": verification.expires_at.isoformat()},
    )
    await db.commit()
    return {
        "verification_id": str(verification.id),
        "status": verification.status,
        "expires_at": verification.expires_at.isoformat(),
        "challenge": challenge,
        "delivery_note": (
            "No outbound email provider is configured. Deliver this challenge to the "
            "requester yourself; it is stored only as a hash and cannot be shown again."
        ),
    }


@router.post("/requests/{request_id}/identity/verify")
async def verify_identity(
    request_id: uuid.UUID,
    payload: VerifyIdentity,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))

    if payload.manual:
        verification = await identity_service.verify_manually(
            db, request,
            reviewer_user_id=uuid.UUID(user.user_id),
            evidence_note=payload.evidence_note or "",
        )
    elif payload.challenge:
        verification = await identity_service.submit_challenge(db, request, payload.challenge)
    else:
        raise CaseNotReadyError("supply either a challenge or manual=true with an evidence note")

    satisfied = identity_service.is_satisfied(verification)
    if satisfied and request.status in (case_vocab.RECEIVED, case_vocab.IDENTITY_PENDING, case_vocab.CLASSIFIED):
        await case_service.transition(
            db, request, case_vocab.IDENTITY_VERIFIED,
            actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_IDENTITY_VERIFIED,
        )
    await db.commit()
    return {
        "status": verification.status,
        "satisfied": satisfied,
        "attempts": verification.attempts,
        "case": await _case_with_identity(db, request),
    }


# ── Search ───────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/search")
async def start_search(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue the subject search.

    The identity gate is checked HERE as well as inside the search itself, so an
    unverified case is refused at the API boundary rather than queueing a job that
    will only fail once a worker picks it up.
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    await identity_service.assert_identity_satisfied(db, request)

    await case_service.transition(
        db, request, case_vocab.SEARCHING,
        actor_user_id=uuid.UUID(user.user_id),
        audit_action=case_vocab.AUDIT_SEARCH_STARTED,
    )
    job = await queue.enqueue(
        db, org_id=request.org_id, job_type="dsr_search",
        payload={"request_id": str(request.id), "org_id": str(request.org_id)},
    )
    await db.commit()
    return {"job_id": str(job.id), "case": await _case_with_identity(db, request)}


@router.get("/requests/{request_id}/results")
async def list_results(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    runs = await dsr_repository.list_search_runs(db, request.id, org_id)
    evidence = await dsr_repository.list_evidence(db, request.id, org_id)
    return {
        "search_runs": [
            {
                "id": str(r.id), "source": r.source_name, "status": r.status,
                "matches": r.match_count, "distinct_subjects": r.distinct_subject_count,
                "tables_searched": r.tables_searched, "error_code": r.error_code,
                "error_detail": r.error_detail,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ],
        "evidence": [
            {
                "id": str(e.id), "source": e.source_name, "table": e.table_name,
                "matched_column": e.matched_column, "identifier_kind": e.identifier_kind,
                "match_type": e.match_type, "confidence": e.confidence,
                "record_reference": e.record_reference, "record_snapshot": e.record_snapshot,
                "observed_at": e.observed_at.isoformat() if e.observed_at else None,
            }
            for e in evidence
        ],
    }


# ── Plan, review, approval ───────────────────────────────────────────────────────

@router.get("/requests/{request_id}/plan")
async def get_plan(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    if plan is None:
        return {"plan": None, "actions": [], "decisions": None}

    actions = await dsr_repository.list_actions(db, plan.id, org_id)
    return {
        "plan": {
            "id": str(plan.id), "version": plan.version, "status": plan.status,
            "summary": plan.summary, "requires_approval": plan.requires_approval,
            "constraints_evaluated": plan.constraints_evaluated,
        },
        "actions": [
            {
                "id": str(a.id), "source": a.source_name, "table": a.table_name,
                "operation": a.operation, "payload": a.operation_payload,
                "reason": a.reason, "expected_result": a.expected_result,
                "risk": a.risk, "requires_approval": a.requires_approval,
                "status": a.status, "blocked_reason": a.blocked_reason,
                "requester_explanation": a.requester_explanation,
                "record_reference": a.record_reference,
            }
            for a in actions
        ],
        "decisions": await approval_service.plan_decision_summary(db, request, plan.id),
    }


@router.post("/requests/{request_id}/actions/{action_id}/decision")
async def decide_action(
    request_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: Decision,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    approval = await approval_service.decide_action(
        db, request, action_id,
        reviewer_user_id=uuid.UUID(user.user_id),
        decision=payload.decision,
        reason=payload.reason,
        edited_payload=payload.edited_payload,
    )

    # Once every action is decided, move the case to APPROVED so execution becomes
    # eligible. Half-decided plans stay put -- executing some actions while others
    # await review is how a requester gets a partial answer nobody chose to give.
    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    summary = await approval_service.plan_decision_summary(db, request, plan.id) if plan else {}
    if summary.get("ready_to_execute") and request.status == case_vocab.APPROVAL_REQUIRED:
        await case_service.transition(
            db, request, case_vocab.APPROVED, actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_APPROVED,
        )
    elif summary.get("nothing_to_execute") and request.status == case_vocab.APPROVAL_REQUIRED:
        # Everything was rejected or blocked. The case still owes the requester an
        # answer explaining that (§47).
        await case_service.transition(
            db, request, case_vocab.RESPONSE_PENDING, actor_user_id=uuid.UUID(user.user_id),
        )
    await db.commit()
    return {
        "approval_id": str(approval.id),
        "decision": approval.decision,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "decisions": summary,
        "case": await _case_with_identity(db, request),
    }


# ── Execution ────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/execute")
async def execute(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue execution of the approved actions.

    Refused unless the case is APPROVED. Safe to call twice: every action claims an
    idempotency key before it does anything, so a duplicate request returns the
    previous outcome rather than performing a deletion again (§25).
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    if request.status != case_vocab.APPROVED:
        raise CaseNotReadyError(
            f"case {request.reference} is {request.status}; execution requires an "
            "approved plan"
        )
    job = await queue.enqueue(
        db, org_id=request.org_id, job_type="dsr_execute",
        payload={"request_id": str(request.id), "org_id": str(request.org_id)},
    )
    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_ACTION_STARTED, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id, after={"job_id": str(job.id)},
    )
    await db.commit()
    return {"job_id": str(job.id), "case": await _case_with_identity(db, request)}


@router.get("/requests/{request_id}/executions")
async def list_executions(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    rows = await dsr_repository.list_executions(db, request.id, org_id)
    return [
        {
            "id": str(e.id), "action_id": str(e.action_id), "status": e.status,
            "rows_affected": e.rows_affected,
            # Deliberately two separate fields: "the write ran" and "a read-back
            # confirmed it" are different claims and the UI must show both (§23).
            "verification_status": e.verification_status,
            "verification_detail": e.verification_detail,
            "verified_at": e.verified_at.isoformat() if e.verified_at else None,
            "error_code": e.error_code, "error_detail": e.error_detail,
            "attempts": e.attempts,
        }
        for e in rows
    ]


# ── Response ─────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/response")
async def generate_response(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    response = await response_service.build_response(
        db, request, actor_user_id=uuid.UUID(user.user_id)
    )
    await db.commit()
    return {
        "id": str(response.id), "version": response.version, "status": response.status,
        "body_text": response.body_text, "grounded_facts": response.grounded_facts,
        "drafted_by_model": response.drafted_by_model,
    }


@router.get("/requests/{request_id}/response")
async def get_response(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    response = await dsr_repository.get_latest_response(db, request.id, org_id)
    if response is None:
        return None
    return {
        "id": str(response.id), "version": response.version, "status": response.status,
        "body_text": response.body_text, "grounded_facts": response.grounded_facts,
        "drafted_by_model": response.drafted_by_model,
        "sent_at": response.sent_at.isoformat() if response.sent_at else None,
    }


@router.post("/requests/{request_id}/complete")
async def complete_case(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark the response sent and close the case.

    Requires a response to exist: a case cannot be completed without the requester
    having been answered, which is why COMPLETED is reachable only from
    RESPONSE_PENDING in the state machine.

    Closes as PARTIALLY_COMPLETED when any action was blocked or failed. Telling a
    requester their case is "completed" when one of their records was retained under
    a policy is telling them something untrue about their own data (§47).
    """
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    response = await dsr_repository.get_latest_response(db, request.id, org_id)
    if response is None:
        raise CaseNotReadyError(
            f"case {request.reference} has no response; generate one before completing it"
        )
    response.status = "sent"
    response.sent_at = datetime.now(UTC)
    response.approved_by_user_id = uuid.UUID(user.user_id)
    await db.flush()

    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    actions = await dsr_repository.list_actions(db, plan.id, org_id) if plan else []
    blocked = sum(1 for a in actions if a.status == "blocked")
    failed = sum(1 for a in actions if a.status == "failed")
    closing = dsr_run_service.closing_status(blocked=blocked, failed=failed)

    await case_service.transition(
        db, request, closing,
        actor_user_id=uuid.UUID(user.user_id),
        audit_action=(
            case_vocab.AUDIT_COMPLETED if closing == case_vocab.COMPLETED
            else case_vocab.AUDIT_PARTIALLY_COMPLETED
        ),
        # PARTIALLY_COMPLETED is one of the statuses the state machine requires a
        # reason for, so the case row says which half did not happen.
        error_code=None if closing == case_vocab.COMPLETED else case_vocab.ERR_ACTION_PARTIAL,
        error_detail=(
            None if closing == case_vocab.COMPLETED
            else f"{blocked} action(s) blocked, {failed} failed; see the plan for reasons"
        ),
        detail={"blocked": blocked, "failed": failed, "response_id": str(response.id)},
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_RESPONSE_SENT, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id,
        after={"response_id": str(response.id), "version": response.version},
    )
    await db.commit()
    return await _case_with_identity(db, request)


# ── Audit timeline ───────────────────────────────────────────────────────────────

@router.get("/requests/{request_id}/audit")
async def case_audit(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The case timeline, read from the SHARED append-only audit_logs table. There is
    no separate DSR event log -- this is the same table Agent 1's decisions land in."""
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    rows = await dsr_repository.list_case_audit(db, request.id, org_id)
    return [
        {
            "id": str(r.id), "action": r.action, "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
            "before": r.before, "after": r.after,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
