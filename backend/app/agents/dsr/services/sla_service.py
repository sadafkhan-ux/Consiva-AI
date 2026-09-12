"""SLA tracking for DSR cases (prompt §36).

No new scheduling framework. The existing worker loop (app/jobs/worker.py) already
runs continuously and already dispatches Agent 1's due scan schedules on each pass;
this adds one more sweep to that same pass.

A breach is recorded, never hidden: `sla_breached` is set and an audit entry is
written, so an organization can see which cases went past their window and when.
Marking the breach does NOT stop the case -- the obligation to answer does not
expire because the deadline did, so the case stays workable and simply carries the
flag.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.schemas import case
from app.db.models import DsrRequest
from app.db.repositories import dsr_repository
from app.services import audit_service

logger = logging.getLogger(__name__)


def remaining(request: DsrRequest, *, now: datetime | None = None):
    """Time left before the response is due. Negative once overdue."""
    return request.due_at - (now or datetime.now(UTC))


def is_overdue(request: DsrRequest, *, now: datetime | None = None) -> bool:
    if request.status in case.TERMINAL_STATUSES:
        return False
    return (now or datetime.now(UTC)) > request.due_at


def sla_view(request: DsrRequest, *, now: datetime | None = None) -> dict:
    """What the UI shows for this case's SLA. One source of truth for the numbers,
    so the frontend never computes its own deadline (§33)."""
    moment = now or datetime.now(UTC)
    left = remaining(request, now=moment)
    return {
        "received_at": request.received_at.isoformat() if request.received_at else None,
        "due_at": request.due_at.isoformat(),
        "remaining_seconds": int(left.total_seconds()),
        "overdue": is_overdue(request, now=moment),
        "breached": bool(request.sla_breached),
        "escalated": request.escalated_at is not None,
        "closed": request.status in case.TERMINAL_STATUSES,
    }


async def sweep_overdue(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Flag every case that has passed its due date. Returns how many were newly
    flagged, for the worker to log.

    Called from the worker's poll loop, so it runs for every tenant -- which is why
    `list_overdue_requests` is the one repository function without an org filter.
    """
    moment = now or datetime.now(UTC)
    overdue = await dsr_repository.list_overdue_requests(db)
    flagged = 0
    for request in overdue:
        if request.sla_breached or request.status in case.TERMINAL_STATUSES:
            continue
        request.sla_breached = True
        flagged += 1
        await db.flush()
        await audit_service.record(
            db,
            org_id=request.org_id,
            actor_user_id=None,
            action=case.AUDIT_SLA_BREACHED,
            entity_type=case.AUDIT_ENTITY,
            entity_id=request.id,
            before={"sla_breached": False},
            after={
                "sla_breached": True,
                "due_at": request.due_at.isoformat(),
                "status_at_breach": request.status,
                "overdue_by_seconds": int((moment - request.due_at).total_seconds()),
            },
        )
        logger.warning(
            "DSR case %s passed its SLA (due %s, status %s)",
            request.reference, request.due_at.isoformat(), request.status,
        )
    return flagged
