import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Action, ConsentFinding, ConsentScan


async def create_action(
    db: AsyncSession, *, org_id: uuid.UUID, finding_id: uuid.UUID, action_type: str, title: str,
    description: str | None, assignee_label: str | None, config_payload: dict | None,
    created_by_user_id: uuid.UUID,
) -> Action:
    action = Action(
        org_id=org_id, finding_id=finding_id, action_type=action_type, title=title, description=description,
        assignee_label=assignee_label, config_payload=config_payload, created_by_user_id=created_by_user_id,
    )
    db.add(action)
    await db.flush()
    return action


async def get_action(db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID) -> Action | None:
    """org_id enforced via a join through consent_findings -> consent_scans, same
    tenant-isolation pattern as finding_repository.get_finding."""
    result = await db.execute(
        select(Action)
        .join(ConsentFinding, Action.finding_id == ConsentFinding.id)
        .join(ConsentScan, ConsentFinding.scan_id == ConsentScan.id)
        .where(Action.id == action_id, ConsentScan.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_actions_for_finding(db: AsyncSession, finding_id: uuid.UUID) -> list[Action]:
    result = await db.execute(select(Action).where(Action.finding_id == finding_id).order_by(Action.created_at))
    return list(result.scalars().all())


async def update_status(db: AsyncSession, action_id: uuid.UUID, status: str) -> Action | None:
    action = await db.get(Action, action_id)
    if action is None:
        return None
    action.status = status
    action.updated_at = datetime.now(UTC)
    if status == "staged":
        action.staged_at = datetime.now(UTC)
    elif status == "live":
        action.deployed_at = datetime.now(UTC)
    await db.flush()
    return action
