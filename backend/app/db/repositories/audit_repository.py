import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRun, AuditLog, ConsentFinding


async def record(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict | None = None,
    after: dict | None = None,
    agent_run_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> AuditLog:
    row = AuditLog(
        org_id=org_id, actor_user_id=actor_user_id, action=action, entity_type=entity_type,
        entity_id=entity_id, before=before, after=after, agent_run_id=agent_run_id, model_name=model_name,
    )
    db.add(row)
    await db.flush()
    return row


async def list_for_scan(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> list[AuditLog]:
    """Audit rows aren't scan-scoped directly — they're reached via the scan itself,
    its agent runs, or its findings. AuditLog carries its own org_id (set at write
    time by record()), so this filters on that directly rather than trusting only the
    caller's earlier get_scan() check."""
    agent_run_ids = select(AgentRun.id).where(AgentRun.scan_id == scan_id)
    finding_ids = select(ConsentFinding.id).where(ConsentFinding.scan_id == scan_id)

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.org_id == org_id,
            or_(
                AuditLog.entity_id == scan_id,
                AuditLog.agent_run_id.in_(agent_run_ids),
                AuditLog.entity_id.in_(finding_ids),
            ),
        )
        .order_by(AuditLog.created_at)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
