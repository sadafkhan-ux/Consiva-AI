"""Incident deadline tracking (§41).

No new scheduling framework. The worker loop already runs continuously and already
sweeps Agent 1's schedules and Agent 3's DSR deadlines; this adds one more pass.

DEADLINES ARE CONFIGURATION, NOT LAW
------------------------------------
§41 is explicit that statutory deadlines must not be hard-coded. What this tracks is
the organisation's own internal response window, stamped at intake from a configurable
value and measured from `detected_at` -- awareness, not paperwork, is what most clocks
run from. Whether a statutory obligation applies, and by when, is a question for a
person against approved guidance, and it belongs in a
`review_notification_duty` action rather than in a timer.

A breach is recorded, never hidden, and does not stop the incident: the obligation to
respond does not expire because a target was missed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.schemas import incident as vocab
from app.db.models import IncidentCase
from app.db.repositories import incident_repository
from app.services import audit_service

logger = logging.getLogger(__name__)


def remaining(case: IncidentCase, *, now: datetime | None = None):
    """Time left before the internal response target. Negative once past it."""
    if case.due_at is None:
        return None
    return case.due_at - (now or datetime.now(UTC))


def is_overdue(case: IncidentCase, *, now: datetime | None = None) -> bool:
    if case.due_at is None or case.status in vocab.TERMINAL_STATUSES:
        return False
    return (now or datetime.now(UTC)) > case.due_at


def sla_view(case: IncidentCase, *, now: datetime | None = None) -> dict:
    """What the UI shows. One source of truth for the numbers, so the frontend never
    computes its own deadline -- two clocks disagreeing about whether an incident is
    overdue is worse than one."""
    moment = now or datetime.now(UTC)
    left = remaining(case, now=moment)
    return {
        "detected_at": case.detected_at.isoformat() if case.detected_at else None,
        "reported_at": case.reported_at.isoformat() if case.reported_at else None,
        "due_at": case.due_at.isoformat() if case.due_at else None,
        "remaining_seconds": int(left.total_seconds()) if left is not None else None,
        "overdue": is_overdue(case, now=moment),
        "breached": bool(case.sla_breached),
        "escalated": case.escalated_at is not None,
        "closed": case.status in vocab.TERMINAL_STATUSES,
        # Said plainly so nobody reads the timer as a legal countdown.
        "note": (
            "Internal response target, measured from detection. Not a statutory "
            "deadline -- notification obligations are a decision for a person against "
            "approved guidance."
        ),
    }


async def sweep_overdue(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Flag every incident past its internal target. Returns how many were newly
    flagged, for the worker to log.

    Runs for every tenant, which is why `list_overdue_incidents` is the one repository
    function without an org filter.
    """
    moment = now or datetime.now(UTC)
    flagged = 0
    for case in await incident_repository.list_overdue_incidents(db):
        if case.sla_breached or case.status in vocab.TERMINAL_STATUSES:
            continue
        case.sla_breached = True
        flagged += 1
        await db.flush()
        await audit_service.record(
            db, org_id=case.org_id, actor_user_id=None,
            action=vocab.AUDIT_SLA_BREACHED, entity_type=vocab.AUDIT_ENTITY,
            entity_id=case.id,
            before={"sla_breached": False},
            after={
                "sla_breached": True,
                "due_at": case.due_at.isoformat() if case.due_at else None,
                "status_at_breach": case.status,
                "overdue_by_seconds": int((moment - case.due_at).total_seconds())
                if case.due_at else None,
            },
        )
        logger.warning(
            "Incident %s passed its internal response target (due %s, status %s)",
            case.reference, case.due_at.isoformat() if case.due_at else "?", case.status,
        )
    return flagged
