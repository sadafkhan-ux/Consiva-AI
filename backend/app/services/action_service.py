"""Action Module (Build Plan Component 8): turns an approved finding into tracked
work. Three action types share one status machine, but which transitions are valid
differs by type — a config_change is the one that must pass through `staged` before
`live`, matching the master reference's "staging preview -> second explicit approval
-> production" requirement; a task/notification has no staging concept.

Honesty note: "notification" here is a real, queryable, audited record — NOT a claim
that an email/Slack message was actually delivered. No outbound provider (SMTP,
webhook) is configured in this project; wiring one is a real, separate future task.
Marking a notification "done" means "handled/acknowledged", not "sent".
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidActionTransitionError, NotFoundError
from app.db.models import Action
from app.db.repositories import action_repository, finding_repository
from app.services import audit_service, notification_service

# Each action_type's allowed status transitions, as {from_status: {allowed_to_statuses}}.
# "cancelled" is reachable from any non-terminal status for every type (handled
# separately below) rather than repeated in each entry.
_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "task": {"open": {"in_progress"}, "in_progress": {"done"}},
    "notification": {"open": {"done"}},
    "config_change": {"open": {"staged"}, "staged": {"live"}},
}
_TERMINAL = {"done", "live", "cancelled"}


async def create_action(
    db: AsyncSession, *, org_id: uuid.UUID, finding_id: uuid.UUID, action_type: str, title: str,
    description: str | None, assignee_label: str | None, config_payload: dict | None,
    created_by_user_id: uuid.UUID,
) -> Action:
    finding = await finding_repository.get_finding(db, finding_id, org_id)
    if finding is None:
        raise NotFoundError(f"Finding {finding_id} not found")
    if finding.status not in ("approved", "edited"):
        # Master reference §2/§8: actions are the "approved decisions become tracked
        # work" step -- creating one against a still-pending or rejected finding would
        # let work start before (or despite) the human gate having actually approved it.
        raise InvalidActionTransitionError(
            f"Finding {finding_id} is not approved/edited (status={finding.status}); "
            "an action can only be created for a decided, approved finding."
        )

    action = await action_repository.create_action(
        db, org_id=org_id, finding_id=finding_id, action_type=action_type, title=title, description=description,
        assignee_label=assignee_label, config_payload=config_payload, created_by_user_id=created_by_user_id,
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=created_by_user_id, action="action.created",
        entity_type="action", entity_id=action.id,
        after={"action_type": action_type, "title": title, "finding_id": str(finding_id)},
    )
    await db.commit()
    return action


async def transition_action(
    db: AsyncSession, *, action_id: uuid.UUID, org_id: uuid.UUID, actor_user_id: uuid.UUID,
    to_status: str, reason: str | None,
) -> Action:
    action = await action_repository.get_action(db, action_id, org_id)
    if action is None:
        raise NotFoundError(f"Action {action_id} not found")
    if action.status in _TERMINAL:
        raise InvalidActionTransitionError(f"Action {action_id} is already {action.status} (terminal)")

    if to_status == "cancelled":
        allowed = True  # cancellable from any non-terminal status, any type
    else:
        allowed = to_status in _TRANSITIONS.get(action.action_type, {}).get(action.status, set())
    if not allowed:
        raise InvalidActionTransitionError(
            f"Cannot transition a {action.action_type} action from {action.status!r} to {to_status!r}"
        )

    # The config_change staged->live step IS the master reference's "second explicit
    # approval" -- a reason is required here specifically, mirroring the same
    # audit-trail rationale as approving a high-risk finding.
    is_production_deploy = action.action_type == "config_change" and action.status == "staged" and to_status == "live"
    if is_production_deploy and not (reason and reason.strip()):
        raise InvalidActionTransitionError("Deploying a config_change to production requires a reason.")

    # A notification only reaches "done" if it was actually delivered -- real HTTP POST
    # to a configured endpoint, not a status flip. dispatch_notification raises (and
    # this function propagates that, leaving the action untouched at "open") if no
    # endpoint is configured or the POST itself fails.
    if action.action_type == "notification" and to_status == "done":
        await notification_service.dispatch_notification(db, action)

    before_status = action.status
    updated = await action_repository.update_status(db, action_id, to_status)
    await audit_service.record(
        db, org_id=org_id, actor_user_id=actor_user_id, action="action.transitioned",
        entity_type="action", entity_id=action_id,
        before={"status": before_status}, after={"status": to_status, "reason": reason},
    )
    await db.commit()
    return updated
