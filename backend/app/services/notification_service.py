"""Real outbound delivery for "notification"-type Actions (master reference §5/§8).

Honesty constraint this module exists to satisfy: a notification action reaching
status="done" must mean an actual HTTP POST to a real, operator-configured endpoint
succeeded — never a fabricated "sent" claim. If no endpoint is configured, or the
POST fails, dispatch raises and the action stays exactly where it was (open),
truthfully reflecting that nothing was delivered.

Any standard "incoming webhook" (Slack, Discord, MS Teams, a generic endpoint) accepts
a POST-JSON-to-a-URL contract, so one configurable URL covers all of them; the payload
shape below additionally nests a Slack-compatible `text` field so pointing this at a
real Slack incoming webhook renders as a readable message with no translation layer.
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import NotificationDeliveryError
from app.db.models import Action
from app.db.repositories import finding_repository


async def dispatch_notification(db: AsyncSession, action: Action) -> None:
    settings = get_settings()
    if not settings.notification_webhook_url:
        raise NotificationDeliveryError(
            "No notification endpoint configured (NOTIFICATION_WEBHOOK_URL is unset) -- "
            "nothing was sent. Point it at a real Slack/Discord/Teams incoming webhook "
            "(or any endpoint accepting POST JSON) to enable real delivery."
        )

    # action.org_id is already the tenant-scoping fact this action was validated
    # against (action_repository.get_action's join), so this reuses that -- not a new
    # unscoped lookup.
    finding = await finding_repository.get_finding(db, action.finding_id, action.org_id)
    finding_summary = finding.finding_text if finding else "(finding not found)"
    risk_level = finding.risk_level if finding else "unknown"

    text = (
        f"[Consiva] {action.title}\n"
        f"Risk: {risk_level} | Assignee: {action.assignee_label or '(unassigned)'}\n"
        f"Finding: {finding_summary}\n"
        f"{action.description or ''}"
    ).strip()
    payload = {
        "text": text,  # Slack/Discord/Teams incoming-webhook-compatible top-level field
        "action_id": str(action.id),
        "finding_id": str(action.finding_id),
        "title": action.title,
        "description": action.description,
        "assignee_label": action.assignee_label,
        "risk_level": risk_level,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.notification_webhook_timeout_seconds) as client:
            response = await client.post(settings.notification_webhook_url, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(f"Notification webhook delivery failed: {exc}") from exc
