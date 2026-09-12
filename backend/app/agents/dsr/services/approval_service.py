"""Human review and approval of a DSR action plan (prompt §22, §23).

APPROVAL IS NOT EXECUTION
-------------------------
This module records that the organization has AUTHORIZED work. It performs none of
it. Nothing here opens a connector, and the only thing it changes at the source is
nothing at all. Execution is execution_service.py, and it re-reads the approvals
written here immediately before it writes (§24) rather than trusting the action
status this module sets.

WHY NOT review_service.py
-------------------------
Agent 1's approval flow is bound to ConsentFinding: `approvals.finding_id` is a NOT
NULL foreign key to consent_findings, and review_service's functions take a
finding_id and resume a LangGraph thread keyed on an agent_run_id. A DSR action is
neither. Agent 2 met the same wall and made the same call. What IS shared, and is
the part that matters for compliance, is `audit_service.record` -- every decision
here lands in the same append-only audit_logs table as every Agent 1 decision.

An approval is not permanent. `expires_at` is stamped at approval time, and
execution refuses an approval that has lapsed -- so a deletion authorized months ago
and never run does not silently execute against data that has since changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.errors import CaseNotReadyError, DsrNotFoundError
from app.agents.dsr.schemas import case
from app.db.models import DsrApproval, DsrRequest
from app.db.repositories import dsr_repository
from app.services import audit_service

# How long an approval authorizes execution for. A deletion approved and then left
# for two months is re-reviewed rather than run against data nobody looked at.
APPROVAL_TTL = timedelta(days=7)

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_MORE_INFO = "request_more_information"
DECISION_EDITED = "edited"
DECISION_ESCALATED = "escalated"

DECISIONS = frozenset({
    DECISION_APPROVED, DECISION_REJECTED, DECISION_MORE_INFO, DECISION_EDITED, DECISION_ESCALATED,
})

# Decisions that must carry a reason. Approving a high-risk action or rejecting one
# without recorded rationale is exactly what the audit trail exists to prevent --
# the same rule Agent 1 applies to a high-risk finding.
_REASON_REQUIRED = frozenset({DECISION_REJECTED, DECISION_MORE_INFO, DECISION_ESCALATED, DECISION_EDITED})


def is_approval_current(approval: DsrApproval | None, *, now: datetime | None = None) -> bool:
    """Whether this approval still authorizes execution right now.

    Used by execution_service immediately before a write. An approval that is
    rejected, superseded, or past its expiry authorizes nothing.
    """
    if approval is None or approval.decision != DECISION_APPROVED:
        return False
    if approval.expires_at is None:
        return True
    return (now or datetime.now(UTC)) <= approval.expires_at


async def decide_action(
    db: AsyncSession,
    request: DsrRequest,
    action_id: uuid.UUID,
    *,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
    now: datetime | None = None,
) -> DsrApproval:
    """Record one reviewer decision on one action.

    Writes the decision, updates the action's status, and audits it -- in that order,
    in one transaction. The caller commits.
    """
    if decision not in DECISIONS:
        raise CaseNotReadyError(f"{decision!r} is not a DSR review decision")
    if decision in _REASON_REQUIRED and not (reason and reason.strip()):
        raise CaseNotReadyError(f"a {decision} decision requires a reason for the audit trail")

    action = await dsr_repository.get_action(db, action_id, request.org_id)
    if action is None or action.request_id != request.id:
        raise DsrNotFoundError(f"action {action_id} not found on case {request.reference}")

    if action.status in ("executed", "executing"):
        raise CaseNotReadyError(
            f"action {action_id} is already {action.status}; a decision cannot be "
            "recorded against work that has started"
        )
    if action.status == "blocked" and decision == DECISION_APPROVED:
        # A constraint blocked this action. A reviewer may escalate or reject it,
        # but "approve" cannot override a retention rule or a missing authorization.
        raise CaseNotReadyError(
            f"action {action_id} is blocked ({action.blocked_reason}); it cannot be "
            "approved until the underlying constraint is resolved"
        )

    moment = now or datetime.now(UTC)
    # The same rule Agent 1 applies to approving a high-risk finding: waving a
    # serious action through with no recorded rationale is what the audit trail
    # exists to prevent.
    high_risk_approval = (
        decision == DECISION_APPROVED and action.requires_approval and action.risk == "high"
    )
    if high_risk_approval and not (reason and reason.strip()):
        raise CaseNotReadyError(
            "approving a high-risk action requires a reason for the audit trail"
        )

    approval = await dsr_repository.record_approval(
        db,
        org_id=request.org_id,
        request_id=request.id,
        action_id=action.id,
        plan_id=action.plan_id,
        reviewer_user_id=reviewer_user_id,
        decision=decision,
        reason=reason,
        edited_payload=edited_payload,
        expires_at=moment + APPROVAL_TTL if decision == DECISION_APPROVED else None,
    )

    before_status = action.status
    action.status = {
        DECISION_APPROVED: "approved",
        DECISION_REJECTED: "rejected",
        DECISION_EDITED: "approved",
        DECISION_MORE_INFO: "proposed",
        DECISION_ESCALATED: "proposed",
    }[decision]
    if decision == DECISION_EDITED and edited_payload:
        action.operation_payload = edited_payload
    await db.flush()

    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=reviewer_user_id,
        action=case.AUDIT_APPROVED if decision == DECISION_APPROVED else case.AUDIT_REJECTED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        before={"action_id": str(action.id), "status": before_status},
        after={
            "action_id": str(action.id), "status": action.status, "decision": decision,
            "reason": reason, "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        },
    )
    return approval


async def plan_decision_summary(
    db: AsyncSession, request: DsrRequest, plan_id: uuid.UUID
) -> dict:
    """Where the plan stands: how many actions are decided, and whether execution
    may begin. `ready_to_execute` is false while ANY action still needs a decision --
    executing half a plan and reviewing the rest afterwards is how a requester ends
    up with a partial answer nobody chose to give them.
    """
    actions = await dsr_repository.list_actions(db, plan_id, request.org_id)
    approved = [a for a in actions if a.status == "approved"]
    rejected = [a for a in actions if a.status == "rejected"]
    blocked = [a for a in actions if a.status == "blocked"]
    undecided = [a for a in actions if a.status == "proposed" and a.requires_approval]
    auto = [a for a in actions if a.status == "proposed" and not a.requires_approval]

    return {
        "total": len(actions),
        "approved": len(approved),
        "rejected": len(rejected),
        "blocked": len(blocked),
        "awaiting_decision": len(undecided),
        "no_approval_needed": len(auto),
        "ready_to_execute": not undecided and bool(approved or auto),
        # Every action is decided but none may run: the case is finished without any
        # source write, and the requester must still be told why.
        "nothing_to_execute": not undecided and not approved and not auto,
    }
