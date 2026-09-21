"""The human decision, and the work that follows it (spec §4 steps 8-10, §15).

Agent 5 produces nothing binding on its own. Everything in this module requires a
named user, and the approvals it writes go into an append-only table -- a decision,
once recorded, cannot be edited or deleted, only followed by another decision.

WHAT "APPROVED" MEANS HERE
--------------------------
Not "this obligation applies". It means: a person read this, agrees it is worth
acting on, and has said so with their name against it. That is the point at which the
platform may raise actions -- and the point BEFORE which it may not, which is why
`open_actions` refuses anything that is not APPROVED.

WHY DISMISSED IS TERMINAL
-------------------------
"This does not apply to us", with a reason and a name, is a position the organisation
took on a date. Re-opening it would erase that. A later change to the same source
raises a NEW finding instead, so the record of what was decided and when survives.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.errors import (
    ApprovalRequiredError,
    InvalidWatchTransitionError,
    WatchNotReadyError,
)
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import lifecycle
from app.db.models import RegWatchAction, RegWatchFinding
from app.db.repositories import regwatch_repository as repo
from app.services import audit_service

logger = logging.getLogger(__name__)

# Every decision that shuts something down, or changes what the agent wrote, has to
# carry a reason. Approving does not: "I read it and it stands" is fully expressed by
# the approval itself, and forcing prose there produces "ok" a thousand times.
_MIN_REASON_CHARS = 10


def _require_reason(decision: str, reason: str | None) -> str | None:
    if decision not in watch.DECISIONS_REQUIRING_REASON:
        return (reason or "").strip() or None
    cleaned = (reason or "").strip()
    if len(cleaned) < _MIN_REASON_CHARS:
        raise WatchNotReadyError(
            f"a {decision!r} decision requires a reason of at least {_MIN_REASON_CHARS} "
            "characters; this decision is part of the compliance record and has to be "
            "explicable to somebody reading it later"
        )
    return cleaned


async def decide(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
) -> RegWatchFinding:
    """Record a reviewer's decision on a finding and move it accordingly."""
    if reviewer_user_id is None:
        raise ApprovalRequiredError(
            "a regulatory finding is decided by a person; no reviewer was supplied"
        )
    if decision not in watch.DECISIONS:
        raise InvalidWatchTransitionError(
            f"{decision!r} is not a decision; expected one of {sorted(watch.DECISIONS)}"
        )
    lifecycle.assert_reviewable(finding.status)
    cleaned_reason = _require_reason(decision, reason)

    before = finding.status
    target: str | None = None

    if decision == watch.DECISION_APPROVE:
        target = watch.APPROVED
    elif decision in (watch.DECISION_DISMISS, watch.DECISION_REJECT):
        target = watch.DISMISSED
    elif decision == watch.DECISION_EDIT:
        # An edit corrects what the agent wrote and then stands as an approval of the
        # corrected version -- there is no state where an edited finding sits waiting
        # for a second person to approve the edit.
        _apply_edits(finding, edited_payload or {})
        target = watch.APPROVED
    elif decision in (watch.DECISION_MORE_INFO, watch.DECISION_ESCALATE):
        # Stays put. The finding is still awaiting a decision -- it has simply been
        # annotated with the fact that somebody looked and wanted more.
        target = None

    await repo.record_approval(
        db,
        org_id=finding.org_id,
        finding_id=finding.id,
        reviewer_user_id=reviewer_user_id,
        subject="finding",
        decision=decision,
        reason=cleaned_reason,
        edited_payload=edited_payload,
    )

    finding.reviewed_by_user_id = reviewer_user_id
    finding.reviewed_at = datetime.now(UTC)
    if target is not None:
        lifecycle.assert_transition(finding.status, target)
        finding.status = target
        # A decided finding no longer sits in the review queue. The flag says "needs a
        # person", and one has now been.
        finding.requires_human_review = False
        if target == watch.DISMISSED:
            finding.closed_at = datetime.now(UTC)
    finding.updated_at = datetime.now(UTC)
    await db.flush()

    action = {
        watch.DECISION_APPROVE: watch.AUDIT_APPROVED,
        watch.DECISION_EDIT: watch.AUDIT_APPROVED,
        watch.DECISION_DISMISS: watch.AUDIT_DISMISSED,
        watch.DECISION_REJECT: watch.AUDIT_DISMISSED,
    }.get(decision, watch.AUDIT_STATUS_CHANGED)
    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=reviewer_user_id,
        action=action,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        before={"status": before},
        after={
            "status": finding.status, "decision": decision, "reason": cleaned_reason,
            "edited": sorted(edited_payload) if edited_payload else [],
        },
    )
    return finding


# The fields a reviewer may correct. Deliberately short: a reviewer edits the AGENT'S
# words and judgement, not the record of what was observed. `relevance_confidence` is
# absent because a human decision is expressed by the decision itself, not by writing
# `confirmed` into a field the agent owns.
EDITABLE_FIELDS = frozenset({"summary", "impact_summary", "priority", "relevance"})


def _apply_edits(finding: RegWatchFinding, payload: dict) -> None:
    unknown = sorted(set(payload) - EDITABLE_FIELDS)
    if unknown:
        raise WatchNotReadyError(
            f"{unknown} cannot be edited on a regulatory finding; editable fields are "
            f"{sorted(EDITABLE_FIELDS)}"
        )
    if "priority" in payload and payload["priority"] not in watch.PRIORITIES:
        raise WatchNotReadyError(
            f"{payload['priority']!r} is not a priority; expected {list(watch.PRIORITIES)}"
        )
    if "relevance" in payload and payload["relevance"] not in watch.RELEVANCE_VALUES:
        raise WatchNotReadyError(
            f"{payload['relevance']!r} is not a relevance value; expected "
            f"{list(watch.RELEVANCE_VALUES)}"
        )
    for field, value in payload.items():
        setattr(finding, field, value)
    if "relevance" in payload:
        # A person changed it, so the confidence attached to it is now a person's,
        # and CONFIRMED is available here -- this is the one place in Agent 5 it is.
        finding.relevance_confidence = watch.CONFIRMED
        finding.relevance_reason = (
            (finding.relevance_reason or "")
            + "\n\nEdited by a reviewer; the relevance above is the reviewer's "
              "position, not the agent's."
        ).strip()


async def open_actions(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    reviewer_user_id: uuid.UUID,
    actions: list[dict],
) -> list[RegWatchAction]:
    """Raise the work a finding calls for. Only on an APPROVED finding.

    Every action is a thing a PERSON does. Nothing here executes anything, and there
    is no `regwatch_act` job for a worker to pick up -- an action is completed by
    somebody attesting that they did it, exactly as Agent 4's containment steps are.
    """
    if finding.status != watch.APPROVED:
        raise WatchNotReadyError(
            f"actions are raised on an approved finding, not on one that is "
            f"{finding.status!r}; approve it first so there is a recorded decision "
            "behind the work"
        )
    if not actions:
        raise WatchNotReadyError("no actions were supplied")

    rows: list[RegWatchAction] = []
    for spec in actions:
        title = (spec.get("title") or "").strip()
        rationale = (spec.get("rationale") or "").strip()
        expected = (spec.get("expected_result") or "").strip()
        if not title or not rationale or not expected:
            raise WatchNotReadyError(
                "every action needs a title, a rationale and an expected result; an "
                "action without them is a task nobody can tell has been done"
            )
        rows.append(RegWatchAction(
            org_id=finding.org_id, finding_id=finding.id,
            title=title, rationale=rationale, expected_result=expected,
            owner_label=(spec.get("owner_label") or "").strip() or None,
            due_at=spec.get("due_at"),
            status=watch.ACTION_OPEN_STATUS,
        ))

    await repo.add_actions(db, finding.org_id, rows)

    lifecycle.assert_transition(finding.status, watch.ACTION_OPEN)
    before = finding.status
    finding.status = watch.ACTION_OPEN
    finding.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=reviewer_user_id,
        action=watch.AUDIT_ACTION_CREATED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        before={"status": before},
        after={
            "status": watch.ACTION_OPEN,
            "actions": [{"id": str(r.id), "title": r.title} for r in rows],
        },
    )
    return rows


async def complete_action(
    db: AsyncSession,
    action: RegWatchAction,
    *,
    actor_user_id: uuid.UUID,
    completed_by: str,
    note: str,
) -> RegWatchAction:
    """Attest that an action was carried out.

    `completed_by` and `note` are both required and both free text, because the
    platform did not do this and cannot verify it. Recording "completed" with nobody's
    name and no description of what was done would be a claim the system cannot
    support -- the same reason Agent 4 records containment as an attestation.
    """
    if action.status in (watch.ACTION_COMPLETED, watch.ACTION_CANCELLED):
        raise InvalidWatchTransitionError(
            f"action is already {action.status!r}; it cannot be completed again"
        )
    who = (completed_by or "").strip()
    what = (note or "").strip()
    if not who or len(what) < _MIN_REASON_CHARS:
        raise WatchNotReadyError(
            "completing an action requires who did it and a note describing what was "
            "done; the platform did not perform this work and cannot verify it"
        )

    before = action.status
    action.status = watch.ACTION_COMPLETED
    action.completed_by = who
    action.completion_note = what
    action.completed_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db, org_id=action.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_ACTION_COMPLETED,
        entity_type=watch.AUDIT_ENTITY, entity_id=action.finding_id,
        before={"action_id": str(action.id), "status": before},
        after={
            "action_id": str(action.id), "status": watch.ACTION_COMPLETED,
            "completed_by": who, "note": what,
            "attested": True,
            "verified_by_platform": False,
        },
    )
    return action


async def close_finding(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    actor_user_id: uuid.UUID,
    note: str | None = None,
) -> RegWatchFinding:
    """Close a finding once the work behind it is done.

    Refuses while any action is still open. Closing over outstanding work would make
    "closed" mean two different things on the same list.
    """
    if finding.status not in (watch.APPROVED, watch.ACTION_OPEN):
        raise WatchNotReadyError(
            f"a finding is closed from {watch.APPROVED} or {watch.ACTION_OPEN}, not "
            f"from {finding.status!r}"
        )
    outstanding = [
        a for a in await repo.list_actions(db, finding.id, finding.org_id)
        if a.status in (watch.ACTION_OPEN_STATUS, watch.ACTION_IN_PROGRESS, watch.ACTION_BLOCKED)
    ]
    if outstanding:
        raise WatchNotReadyError(
            f"{len(outstanding)} action(s) on this finding are still open; complete or "
            "cancel them before closing, so that 'closed' means the same thing on "
            "every row of the list"
        )

    before = finding.status
    lifecycle.assert_transition(finding.status, watch.CLOSED)
    finding.status = watch.CLOSED
    finding.closed_at = datetime.now(UTC)
    finding.requires_human_review = False
    finding.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_CLOSED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        before={"status": before},
        after={"status": watch.CLOSED, "note": note},
    )
    return finding
