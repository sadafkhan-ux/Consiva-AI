"""Actions that have passed the date somebody set for them (spec §4 step 10).

`due_at` was stored on every action and shown in the API from the day actions were
built, and nothing ever looked at it. A date that is recorded, displayed, and never
checked is worse than no date at all: it reads as a commitment the system is tracking
when the system is tracking nothing.

WHAT THIS IS, AND WHAT IT IS EMPHATICALLY NOT
---------------------------------------------
It is the organisation's OWN target for work it gave itself. Somebody wrote "review
the consent notice by the 30th", and this reports when the 30th has passed.

It is NOT a statutory deadline, and nothing here computes one. Whether a regulatory
change carries a legal deadline, and what that deadline is, is a determination made
by a person against approved guidance -- exactly as Agent 4 treats an incident
response target. The note travels with every row so no surface has to remember the
caveat on its own, and so nobody reads "overdue" as "in breach".

The sweep does not escalate, reassign or chase. It marks, and it reports. An agent
that started moving other people's work around because a date passed would be making
decisions this platform does not make.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.schemas import watch
from app.db.models import RegWatchAction, RegWatchFinding
from app.services import audit_service

logger = logging.getLogger(__name__)

# Statuses where a due date still means something. A completed or cancelled action has
# an ending already; reporting it as overdue would be reporting on finished work.
LIVE_STATUSES = frozenset({
    watch.ACTION_OPEN_STATUS, watch.ACTION_IN_PROGRESS, watch.ACTION_BLOCKED,
})

# How close to the date counts as "due soon". A week, because these are policy and
# process tasks measured in working days, not minutes.
DUE_SOON_WINDOW = timedelta(days=7)

NOT_A_LEGAL_DEADLINE = (
    "This is the date this organisation set for its own work. It is not a statutory "
    "deadline, and passing it is not a finding that any legal obligation has been "
    "missed."
)


def is_overdue(action: RegWatchAction, *, now: datetime | None = None) -> bool:
    if action.due_at is None or action.status not in LIVE_STATUSES:
        return False
    return (now or datetime.now(UTC)) > action.due_at


def is_due_soon(action: RegWatchAction, *, now: datetime | None = None) -> bool:
    if action.due_at is None or action.status not in LIVE_STATUSES:
        return False
    moment = now or datetime.now(UTC)
    return moment <= action.due_at <= moment + DUE_SOON_WINDOW


def view(action: RegWatchAction, *, now: datetime | None = None) -> dict:
    """What the API and the console both read, computed in one place.

    `state` is deliberately four-valued rather than a boolean. "No date was set" and
    "a date was set and there is time left" are different facts, and collapsing them
    into `overdue: false` loses the first one entirely -- an action nobody put a date
    on looks identical to one comfortably on track.
    """
    moment = now or datetime.now(UTC)
    if action.due_at is None:
        state, note = "no_date", "No target date was set for this action."
    elif action.status not in LIVE_STATUSES:
        state, note = "settled", f"This action is {action.status}; its date no longer applies."
    elif moment > action.due_at:
        state, note = "overdue", (
            f"Past the target date set for it. {NOT_A_LEGAL_DEADLINE}"
        )
    elif is_due_soon(action, now=moment):
        state, note = "due_soon", f"Due within {DUE_SOON_WINDOW.days} days. {NOT_A_LEGAL_DEADLINE}"
    else:
        state, note = "on_track", NOT_A_LEGAL_DEADLINE

    return {
        "state": state,
        "overdue": state == "overdue",
        "due_at": action.due_at.isoformat() if action.due_at else None,
        "remaining_seconds": (
            int((action.due_at - moment).total_seconds()) if action.due_at else None
        ),
        "note": note,
        "is_statutory_deadline": False,
    }


async def sweep_overdue(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Record, once, that an action has passed its date. Returns how many were newly marked.

    Deliberately unscoped by organisation, like the DSR and incident sweeps beside it
    on the worker's maintenance loop: a sweep that could only see one tenant would
    silently stop watching every other one.

    "Once" matters. The audit log is append-only and this runs every maintenance tick,
    so a sweep that wrote on every pass would bury the record of every real decision
    under thousands of identical rows within a day. An action already marked is
    skipped.
    """
    moment = now or datetime.now(UTC)
    result = await db.execute(
        select(RegWatchAction).where(
            RegWatchAction.due_at.is_not(None),
            RegWatchAction.due_at < moment,
            RegWatchAction.status.in_(tuple(LIVE_STATUSES)),
        )
    )
    newly_marked = 0
    for action in result.scalars().all():
        if await _already_marked(db, action):
            continue
        await audit_service.record(
            db, org_id=action.org_id, actor_user_id=None,
            action=watch.AUDIT_STATUS_CHANGED,
            entity_type=watch.AUDIT_ENTITY, entity_id=action.finding_id,
            after={
                "action_id": str(action.id),
                "title": action.title,
                "overdue": True,
                "due_at": action.due_at.isoformat() if action.due_at else None,
                "status": action.status,
                # Carried in the record itself, not only in this module's docstring.
                "note": NOT_A_LEGAL_DEADLINE,
                "is_statutory_deadline": False,
            },
        )
        newly_marked += 1

    if newly_marked:
        logger.warning(
            "Regulatory watch: %d action(s) newly past the target date set for them "
            "(internal targets, not statutory deadlines)",
            newly_marked,
        )
    return newly_marked


async def _already_marked(db: AsyncSession, action: RegWatchAction) -> bool:
    """Whether this action has been reported overdue before.

    Read from the audit log rather than from a flag on the row, because the audit log
    is the thing that must not be written twice and is therefore the honest place to
    ask. It also means no migration and no column that could drift from the log.
    """
    from app.db.models import AuditLog

    result = await db.execute(
        select(AuditLog.id).where(
            AuditLog.org_id == action.org_id,
            AuditLog.entity_type == watch.AUDIT_ENTITY,
            AuditLog.entity_id == action.finding_id,
            AuditLog.action == watch.AUDIT_STATUS_CHANGED,
            AuditLog.after["action_id"].astext == str(action.id),
            AuditLog.after["overdue"].astext == "true",
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def overdue_for_org(
    db: AsyncSession, org_id: uuid.UUID, *, now: datetime | None = None
) -> list[dict]:
    """Every live action in this org that has passed its date, for the dashboard."""
    moment = now or datetime.now(UTC)
    result = await db.execute(
        select(RegWatchAction, RegWatchFinding.reference)
        .join(RegWatchFinding, RegWatchFinding.id == RegWatchAction.finding_id)
        .where(
            RegWatchAction.org_id == org_id,
            RegWatchAction.due_at.is_not(None),
            RegWatchAction.due_at < moment,
            RegWatchAction.status.in_(tuple(LIVE_STATUSES)),
        )
        .order_by(RegWatchAction.due_at)
    )
    return [
        {
            "id": str(action.id),
            "finding_id": str(action.finding_id),
            "finding_reference": reference,
            "title": action.title,
            "owner_label": action.owner_label,
            "status": action.status,
            **view(action, now=moment),
        }
        for action, reference in result.all()
    ]
