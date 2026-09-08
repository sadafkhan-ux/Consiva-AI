import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunStage, ConsentScan


async def start_stage(
    db: AsyncSession, *, scan_id: uuid.UUID, stage: str, agent_run_id: uuid.UUID | None = None
) -> AgentRunStage:
    row = AgentRunStage(
        scan_id=scan_id, agent_run_id=agent_run_id, stage=stage,
        status="running", started_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def complete_stage(db: AsyncSession, stage_id: uuid.UUID, *, duration_ms: int, metadata: dict) -> None:
    row = await db.get(AgentRunStage, stage_id)
    if row:
        row.status = "completed"
        row.completed_at = datetime.now(UTC)
        row.duration_ms = duration_ms
        row.stage_metadata = metadata
        await db.flush()


async def fail_stage(db: AsyncSession, stage_id: uuid.UUID, *, duration_ms: int, error: str) -> None:
    row = await db.get(AgentRunStage, stage_id)
    if row:
        row.status = "failed"
        row.completed_at = datetime.now(UTC)
        row.duration_ms = duration_ms
        row.error = error
        await db.flush()


async def list_stages(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> list[AgentRunStage]:
    """org_id is enforced here (via a join to consent_scans -- AgentRunStage has no
    org_id of its own), not just by the caller's earlier get_scan() check."""
    result = await db.execute(
        select(AgentRunStage)
        .join(ConsentScan, AgentRunStage.scan_id == ConsentScan.id)
        .where(AgentRunStage.scan_id == scan_id, ConsentScan.org_id == org_id)
        .order_by(AgentRunStage.created_at)
    )
    return list(result.scalars().all())
