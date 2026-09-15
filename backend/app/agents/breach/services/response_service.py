"""Response planning, approval and controlled execution (§21-§25).

RECOMMENDATION IS NOT ACTION
---------------------------
The planner proposes. A reviewer authorises. Somebody then does the work, or a
connector does it. Each of those is a separate step with its own record, and the
agent "must not silently execute high-impact security actions" (§21).

TRACKED VERSUS CONNECTOR, AND WHY IT MATTERS
--------------------------------------------
Consiva has no connector to Active Directory, a cloud console or a firewall. It cannot
disable an account, rotate a credential or isolate a service, and building buttons
that appear to do so would be the fake implementation §50 forbids.

So a TRACKED action is work Consiva assigns, approves, tracks and audits, which a
person performs elsewhere and then attests to. The attestation is recorded as exactly
what it is -- somebody's word -- and never dressed up as verification.

A CONNECTOR action is the narrow case where the containment genuinely is a database
operation on a source already authorized for DSR. There Agent 3's connector does the
work for real and reads the record back, and that IS verification.

`verification_status` keeps the two apart: `attested` and `read_back` are different
claims, and an incident report that blurred them would overstate what is known.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.errors import (
    ActionBlockedError,
    ApprovalRequiredError,
    IncidentNotFoundError,
    IncidentNotReadyError,
)
from app.agents.breach.schemas import incident as vocab
from app.db.models import IncidentAction, IncidentCase, IncidentExecution
from app.db.repositories import incident_repository
from app.services import audit_service

logger = logging.getLogger(__name__)

# How long an approval authorises work for. Containment approved during an incident
# and not carried out within a day is re-reviewed: the situation moves, and an
# authorisation given on Monday's understanding should not license Friday's action.
APPROVAL_TTL = timedelta(hours=24)


# ── Planning (§21) ──────────────────────────────────────────────────────────────
#
# A playbook per incident type. These are RECOMMENDATIONS drawn from ordinary incident
# response practice, not legal requirements and not the only correct answer -- a
# reviewer is expected to edit, reject and add.
_PLAYBOOK: dict[str, tuple[tuple[str, str, str], ...]] = {
    vocab.TYPE_CREDENTIAL_COMPROMISE: (
        (vocab.ACT_DISABLE_ACCOUNT, "Disable the compromised account",
         "A credential known to be in someone else's hands keeps working until it is stopped."),
        (vocab.ACT_REVOKE_SESSION, "Revoke active sessions for the account",
         "Disabling an account does not end sessions already issued against it."),
        (vocab.ACT_ROTATE_CREDENTIAL, "Rotate the credential and any it shared",
         "A reused password compromises every account that shares it."),
        (vocab.ACT_INVESTIGATE, "Review what the account accessed while compromised",
         "The access history bounds what data was actually reachable."),
        (vocab.ACT_PRESERVE_LOGS, "Preserve authentication and access logs",
         "Log retention windows are short and evidence is lost by default, not by decision."),
    ),
    vocab.TYPE_UNAUTHORIZED_ACCESS: (
        (vocab.ACT_PRESERVE_LOGS, "Preserve access and query logs",
         "Establishing scope later depends entirely on logs kept now."),
        (vocab.ACT_INVESTIGATE, "Determine what was read and how much",
         "Scope drives every downstream decision, including whether anyone must be told."),
        (vocab.ACT_REVOKE_SESSION, "Revoke sessions on the affected system",
         "Ends access that is still live while the investigation runs."),
        (vocab.ACT_REVIEW_NOTIFICATION_DUTY, "Review whether anyone must be notified",
         "A question for a person against approved guidance, not for this agent."),
    ),
    vocab.TYPE_DATA_EXPOSURE: (
        (vocab.ACT_ISOLATE_SERVICE, "Close the exposure",
         "Nothing else matters while the data is still reachable."),
        (vocab.ACT_PRESERVE_LOGS, "Preserve access logs for the exposed resource",
         "Who reached it, and how often, is the difference between exposure and breach."),
        (vocab.ACT_INVESTIGATE, "Establish whether the exposure was accessed",
         "Reachable and retrieved are different incidents."),
        (vocab.ACT_REVIEW_NOTIFICATION_DUTY, "Review whether anyone must be notified",
         "A question for a person against approved guidance."),
    ),
    vocab.TYPE_MISCONFIGURATION: (
        (vocab.ACT_PATCH_MISCONFIGURATION, "Correct the configuration",
         "The exposure continues until the setting changes."),
        (vocab.ACT_PRESERVE_LOGS, "Preserve access logs before they roll over",
         "Misconfigurations are usually found long after they began."),
        (vocab.ACT_INVESTIGATE, "Establish how long it was open and who reached it",
         "Duration and access together determine the actual impact."),
    ),
    vocab.TYPE_DATA_LEAKAGE: (
        (vocab.ACT_PRESERVE_LOGS, "Preserve all relevant logs immediately",
         "Exfiltration cases become legal matters; evidence handling matters from hour one."),
        (vocab.ACT_INVESTIGATE, "Establish what left and by what route",
         "Bounds the impact and often identifies the remaining hole."),
        (vocab.ACT_ISOLATE_SERVICE, "Close the route used",
         "Prevents the same path being used again while the investigation runs."),
        (vocab.ACT_REVIEW_NOTIFICATION_DUTY, "Review notification obligations urgently",
         "Confirmed exfiltration of personal data is the case where obligations most often apply."),
    ),
    vocab.TYPE_MALWARE: (
        (vocab.ACT_ISOLATE_SERVICE, "Isolate the affected hosts",
         "Containing spread takes precedence over investigating it."),
        (vocab.ACT_PRESERVE_LOGS, "Preserve system and network logs",
         "Reimaging destroys the evidence of what happened."),
        (vocab.ACT_INVESTIGATE, "Establish whether data was accessed or taken",
         "Ransomware is frequently accompanied by exfiltration."),
        (vocab.ACT_ROTATE_CREDENTIAL, "Rotate credentials used on affected hosts",
         "Assume anything present on a compromised host is compromised."),
    ),
    vocab.TYPE_INSIDER: (
        (vocab.ACT_PRESERVE_LOGS, "Preserve access and transfer logs",
         "Insider cases usually become employment or legal matters."),
        (vocab.ACT_REVOKE_SESSION, "Review and revoke the individual's access",
         "Scope the access before changing it, or the evidence goes with it."),
        (vocab.ACT_INVESTIGATE, "Establish what was accessed and moved",
         "Determines both impact and whether it was within their normal duties."),
    ),
    vocab.TYPE_LOST_DEVICE: (
        (vocab.ACT_REVOKE_SESSION, "Revoke the device's sessions and tokens",
         "A lost device is only a data incident if what is on it remains usable."),
        (vocab.ACT_INVESTIGATE, "Establish what was stored on it and whether it was encrypted",
         "Encryption at rest is usually the difference between an incident and a breach."),
        (vocab.ACT_ROTATE_CREDENTIAL, "Rotate credentials cached on the device",
         "Saved credentials outlive the hardware."),
    ),
    vocab.TYPE_THIRD_PARTY: (
        (vocab.ACT_INVESTIGATE, "Establish what the vendor holds and what they report",
         "Impact depends on what was shared with them, which is Consiva's own record."),
        (vocab.ACT_NOTIFY_INTERNAL, "Brief the privacy and vendor-management owners",
         "A processor's incident is still the controller's obligation."),
        (vocab.ACT_REVIEW_NOTIFICATION_DUTY, "Review notification obligations",
         "A question for a person against approved guidance."),
    ),
    vocab.TYPE_ACCIDENTAL_DISCLOSURE: (
        (vocab.ACT_INVESTIGATE, "Establish what was disclosed and to whom",
         "Recipient and content determine whether recall is worth attempting."),
        (vocab.ACT_NOTIFY_INTERNAL, "Brief the privacy owner",
         "Small disclosures still carry obligations."),
    ),
}

# Every incident gets these, whatever its type.
_UNIVERSAL: tuple[tuple[str, str, str], ...] = (
    (vocab.ACT_NOTIFY_INTERNAL, "Brief the internal incident owners",
     "The people accountable for the decision need to know it is being made."),
)

_RISK_FOR_ACTION: dict[str, str] = {
    vocab.ACT_DISABLE_ACCOUNT: "high",
    vocab.ACT_ISOLATE_SERVICE: "high",
    vocab.ACT_ROTATE_CREDENTIAL: "high",
    vocab.ACT_PATCH_MISCONFIGURATION: "medium",
    vocab.ACT_REVOKE_SESSION: "medium",
    vocab.ACT_PRESERVE_LOGS: "low",
    vocab.ACT_INVESTIGATE: "low",
    vocab.ACT_NOTIFY_INTERNAL: "low",
    vocab.ACT_REVIEW_NOTIFICATION_DUTY: "low",
    vocab.ACT_OTHER: "medium",
}


async def build_response_plan(
    db: AsyncSession,
    case: IncidentCase,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> list[IncidentAction]:
    """Propose a containment plan for this incident type.

    Everything is proposed; nothing is done. Planning again adds only what is missing,
    rather than duplicating actions a reviewer has already decided on -- an incident
    is re-planned as understanding changes, and losing existing decisions each time
    would make the plan useless.
    """
    existing = await incident_repository.list_actions(db, case.id, case.org_id)
    already = {(a.action_kind, a.title) for a in existing}

    proposed: list[IncidentAction] = []
    for kind, title, rationale in _PLAYBOOK.get(case.incident_type, ()) + _UNIVERSAL:
        if (kind, title) in already:
            continue
        risk = _RISK_FOR_ACTION.get(kind, "medium")
        proposed.append(IncidentAction(
            org_id=case.org_id, incident_id=case.id, action_kind=kind,
            # Every playbook action is tracked work: none of these is a database
            # operation Consiva can perform. A connector action is added deliberately,
            # never inferred from a type.
            execution_mode=vocab.EXECUTION_MODE_TRACKED,
            title=title, rationale=rationale,
            expected_result=_expected_result(kind),
            risk=risk,
            requires_approval=kind in vocab.HIGH_IMPACT_ACTIONS or risk == "high",
            status="proposed",
        ))

    if proposed:
        await incident_repository.add_actions(db, case.org_id, proposed)
        await audit_service.record(
            db, org_id=case.org_id, actor_user_id=actor_user_id,
            action=vocab.AUDIT_ACTION_PLANNED, entity_type=vocab.AUDIT_ENTITY,
            entity_id=case.id,
            after={
                "proposed": len(proposed),
                "kinds": sorted({a.action_kind for a in proposed}),
                "incident_type": case.incident_type,
                "note": "recommendations only; nothing has been performed",
            },
        )
    return proposed


def _expected_result(kind: str) -> str:
    return {
        vocab.ACT_DISABLE_ACCOUNT: "the account can no longer authenticate",
        vocab.ACT_REVOKE_SESSION: "existing sessions and tokens no longer work",
        vocab.ACT_ROTATE_CREDENTIAL: "the old credential no longer works and the new one is in use",
        vocab.ACT_ISOLATE_SERVICE: "the affected service is unreachable by the route used",
        vocab.ACT_PATCH_MISCONFIGURATION: "the setting is corrected and verified",
        vocab.ACT_PRESERVE_LOGS: "relevant logs are copied somewhere retention cannot expire them",
        vocab.ACT_INVESTIGATE: "a written finding, with the evidence it rests on, on this incident",
        vocab.ACT_NOTIFY_INTERNAL: "the named internal owners have acknowledged the brief",
        vocab.ACT_REVIEW_NOTIFICATION_DUTY: (
            "a recorded decision on notification obligations, taken by a person against "
            "approved guidance"
        ),
    }.get(kind, "the containment step is complete and recorded")


async def add_action(
    db: AsyncSession,
    case: IncidentCase,
    *,
    action_kind: str,
    title: str,
    rationale: str,
    expected_result: str,
    execution_mode: str = vocab.EXECUTION_MODE_TRACKED,
    target: str | None = None,
    assignee_label: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> IncidentAction:
    """Add one action a reviewer decided on themselves."""
    if action_kind not in vocab.ACTION_KINDS:
        raise IncidentNotReadyError(f"{action_kind!r} is not a response action")
    if execution_mode not in vocab.EXECUTION_MODES:
        raise IncidentNotReadyError(f"{execution_mode!r} is not an execution mode")
    if not (title.strip() and rationale.strip()):
        raise IncidentNotReadyError(
            "an action needs a title and a rationale -- a containment step nobody can "
            "evaluate cannot be approved"
        )

    risk = _RISK_FOR_ACTION.get(action_kind, "medium")
    row = IncidentAction(
        org_id=case.org_id, incident_id=case.id, action_kind=action_kind,
        execution_mode=execution_mode, title=title.strip(), rationale=rationale.strip(),
        expected_result=expected_result.strip(), target=target,
        assignee_label=assignee_label, risk=risk,
        requires_approval=action_kind in vocab.HIGH_IMPACT_ACTIONS or risk == "high",
        status="proposed",
    )
    await incident_repository.add_actions(db, case.org_id, [row])
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_ACTION_PLANNED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"action_id": str(row.id), "kind": action_kind, "title": row.title,
               "execution_mode": execution_mode, "added_by": "reviewer"},
    )
    return row


# ── Approval (§22, §23) ─────────────────────────────────────────────────────────

def is_approval_current(approval, *, now: datetime | None = None) -> bool:
    """Whether this approval still authorises work right now.

    Re-read immediately before acting. An approval that is rejected, superseded or
    past its expiry authorises nothing.
    """
    if approval is None or approval.decision != vocab.DECISION_APPROVE:
        return False
    if approval.expires_at is None:
        return True
    return (now or datetime.now(UTC)) <= approval.expires_at


async def decide_action(
    db: AsyncSession,
    case: IncidentCase,
    action_id: uuid.UUID,
    *,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None = None,
    now: datetime | None = None,
):
    """Record one reviewer decision on one containment action."""
    if decision not in vocab.DECISIONS:
        raise IncidentNotReadyError(f"{decision!r} is not a review decision")
    if decision in vocab.DECISIONS_REQUIRING_REASON and not (reason and reason.strip()):
        raise IncidentNotReadyError(f"a {decision} decision requires a reason")

    action = await incident_repository.get_action(db, action_id, case.org_id)
    if action is None or action.incident_id != case.id:
        raise IncidentNotFoundError(f"action {action_id} not found on {case.reference}")
    if action.status in ("in_progress", "completed"):
        raise IncidentNotReadyError(
            f"action {action_id} is already {action.status}; a decision cannot be "
            "recorded against work that has started"
        )

    # Approving a high-risk containment without a recorded rationale is what the audit
    # trail exists to prevent. Disabling the wrong account during an incident is its
    # own incident.
    if decision == vocab.DECISION_APPROVE and action.risk == "high" and not (reason and reason.strip()):
        raise IncidentNotReadyError(
            "approving a high-risk containment action requires a reason for the audit trail"
        )

    moment = now or datetime.now(UTC)
    approval = await incident_repository.record_approval(
        db, org_id=case.org_id, incident_id=case.id, reviewer_user_id=reviewer_user_id,
        subject="action", decision=decision, action_id=action.id, reason=reason,
        expires_at=moment + APPROVAL_TTL if decision == vocab.DECISION_APPROVE else None,
    )

    before = action.status
    action.status = {
        vocab.DECISION_APPROVE: "approved",
        vocab.DECISION_REJECT: "rejected",
        vocab.DECISION_EDIT: "approved",
        vocab.DECISION_MORE_INFO: "proposed",
        vocab.DECISION_ESCALATE: "proposed",
    }[decision]
    await db.flush()

    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=reviewer_user_id,
        action=vocab.AUDIT_APPROVED if decision == vocab.DECISION_APPROVE else vocab.AUDIT_REJECTED,
        entity_type=vocab.AUDIT_ENTITY, entity_id=case.id,
        before={"action_id": str(action.id), "status": before},
        after={
            "action_id": str(action.id), "status": action.status, "decision": decision,
            "reason": reason,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        },
    )
    return approval


async def plan_summary(db: AsyncSession, case: IncidentCase) -> dict:
    """Where the plan stands, and whether containment may begin."""
    actions = await incident_repository.list_actions(db, case.id, case.org_id)
    approved = [a for a in actions if a.status == "approved"]
    undecided = [a for a in actions if a.status == "proposed" and a.requires_approval]
    auto = [a for a in actions if a.status == "proposed" and not a.requires_approval]
    return {
        "total": len(actions),
        "approved": len(approved),
        "rejected": sum(1 for a in actions if a.status == "rejected"),
        "blocked": sum(1 for a in actions if a.status == "blocked"),
        "completed": sum(1 for a in actions if a.status == "completed"),
        "failed": sum(1 for a in actions if a.status == "failed"),
        "awaiting_decision": len(undecided),
        "no_approval_needed": len(auto),
        "ready_to_respond": not undecided and bool(approved or auto),
        "nothing_to_do": not undecided and not approved and not auto,
    }


# ── Controlled execution (§24, §25) ─────────────────────────────────────────────

def idempotency_key(action: IncidentAction) -> str:
    """Derived from WHAT is being done, not from when it was asked for.

    Disabling an account twice is usually harmless; rotating a credential twice can
    lock out the very people trying to respond, which is why this exists.
    """
    material = "|".join([
        str(action.id), action.action_kind, action.execution_mode, action.target or "",
    ])
    return hashlib.sha256(material.encode()).hexdigest()


async def _preflight(
    db: AsyncSession, case: IncidentCase, action_id: uuid.UUID, *, now: datetime
) -> IncidentAction:
    """The checks §24 requires, done at the moment of acting rather than reused from
    when the plan was approved."""
    action = await incident_repository.get_action(db, action_id, case.org_id)
    if action is None or action.incident_id != case.id:
        raise IncidentNotFoundError(f"action {action_id} not found on {case.reference}")

    if case.status not in (vocab.APPROVED, vocab.RESPONDING):
        raise ApprovalRequiredError(
            f"incident {case.reference} is {case.status}; containment may only be "
            f"carried out from {vocab.APPROVED} or {vocab.RESPONDING}"
        )
    if action.status == "blocked":
        raise ActionBlockedError(f"action {action_id} is blocked: {action.blocked_reason}")
    if action.status == "completed":
        raise ActionBlockedError(f"action {action_id} is already complete")

    if action.requires_approval:
        approval = await incident_repository.latest_approval_for_action(
            db, action.id, case.org_id
        )
        if not is_approval_current(approval, now=now):
            raise ApprovalRequiredError(
                f"action {action_id} has no current approval "
                f"(latest: {approval.decision if approval else 'none'}); "
                "containment is refused"
            )
    return action


async def record_tracked_execution(
    db: AsyncSession,
    case: IncidentCase,
    action_id: uuid.UUID,
    *,
    performed_by: str,
    attestation: str,
    actor_user_id: uuid.UUID,
    now: datetime | None = None,
) -> IncidentExecution:
    """Record that a person performed a tracked action outside Consiva.

    This is an ATTESTATION, not a verification. Consiva did not watch the account being
    disabled and cannot check that it was; `verification_status='attested'` says
    exactly that. Both fields are mandatory because "somebody did it" with no name and
    no description is not a record of anything.
    """
    moment = now or datetime.now(UTC)
    action = await _preflight(db, case, action_id, now=moment)

    if action.execution_mode != vocab.EXECUTION_MODE_TRACKED:
        raise IncidentNotReadyError(
            f"action {action_id} is a {action.execution_mode} action; it is performed "
            "by a connector rather than attested"
        )
    if not (performed_by and performed_by.strip()):
        raise IncidentNotReadyError("an attestation must name who performed the action")
    if not (attestation and attestation.strip()):
        raise IncidentNotReadyError(
            "an attestation must say what was actually done -- a tick with no "
            "description is not a record of anything"
        )

    execution, is_new = await incident_repository.claim_execution(
        db, org_id=case.org_id, incident_id=case.id, action_id=action.id,
        idempotency_key=idempotency_key(action),
        execution_mode=vocab.EXECUTION_MODE_TRACKED,
        executed_by_user_id=actor_user_id,
    )
    if not is_new:
        logger.info(
            "Action %s was already recorded as performed; returning the existing record",
            action.id,
        )
        return execution

    execution.attempts += 1
    execution.started_at = moment
    execution.performed_by = performed_by.strip()
    execution.attestation = attestation.strip()
    execution.status = "succeeded"
    # Deliberately NOT 'read_back'. Nothing machine-checked this.
    execution.verification_status = "attested"
    execution.verification_detail = {
        "kind": "human attestation",
        "note": "Consiva did not observe this action and cannot confirm its effect",
    }
    execution.completed_at = moment
    action.status = "completed"
    await db.flush()

    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_ACTION_EXECUTED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "action_id": str(action.id), "execution_id": str(execution.id),
            "kind": action.action_kind, "execution_mode": "tracked",
            "performed_by": execution.performed_by,
            "verification": "attested",
        },
    )
    return execution


async def mark_action_failed(
    db: AsyncSession,
    case: IncidentCase,
    action_id: uuid.UUID,
    *,
    reason: str,
    actor_user_id: uuid.UUID,
    now: datetime | None = None,
) -> IncidentAction:
    """Record that a containment action could not be carried out.

    An action somebody tried and could not complete is a different thing from one
    nobody has started, and the difference changes what happens next -- it is usually
    the point at which an incident should escalate.
    """
    if not (reason and reason.strip()):
        raise IncidentNotReadyError("a failed action must say why it failed")

    action = await incident_repository.get_action(db, action_id, case.org_id)
    if action is None or action.incident_id != case.id:
        raise IncidentNotFoundError(f"action {action_id} not found on {case.reference}")

    action.status = "failed"
    action.blocked_reason = reason.strip()
    await db.flush()
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_ACTION_EXECUTED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"action_id": str(action.id), "status": "failed", "reason": action.blocked_reason},
    )
    return action
