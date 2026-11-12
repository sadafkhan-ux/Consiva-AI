import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
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


async def fail_stage(
    db: AsyncSession, stage_id: uuid.UUID, *, duration_ms: int, error: str,
    metadata: dict | None = None,
) -> None:
    """Records the failure AND whatever the stage managed to observe before it failed.

    `metadata` used to be dropped here: track_stage collected it, passed it down, and
    only complete_stage ever wrote it -- so every diagnostic a stage recorded was
    discarded on exactly the runs where it was needed. A real llm_analysis timeout
    stored duration and "Request timed out." and nothing else, while
    `provider_failure`, `degraded_to_rule_findings`, `prompt_chars`, `llm_usage` and
    the evidence counts had all been set and were thrown away. Defaulted to None so
    existing callers keep working unchanged."""
    row = await db.get(AgentRunStage, stage_id)
    if row:
        row.status = "failed"
        row.completed_at = datetime.now(UTC)
        row.duration_ms = duration_ms
        row.error = error
        if metadata:
            row.stage_metadata = metadata
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


#: A stage that has been "running" this long, of a scan that has since finished, will never report its end (the worker that ran it was stopped or restarted mid-stage).
ORPHAN_STAGE_AFTER = timedelta(hours=2)
ORPHAN_STAGE_ERROR = "The stage never reported its end; its scan had already finished, so it is closed here."


async def close_orphan_stages(db: AsyncSession, *, older_than: timedelta = ORPHAN_STAGE_AFTER) -> int:
    """Closes stages left "running" long after their scan finished, so the progress shown for a finished scan never has a stage still spinning. Only stages of a scan that is
    completed, failed or cancelled are touched, and only once they are older than `older_than`: a stage of a scan that is still being worked on is never closed. Returns how many."""
    finished = select(ConsentScan.id).where(ConsentScan.status.in_(("completed", "failed", "cancelled")))
    cutoff = datetime.now(UTC) - older_than
    result = await db.execute(
        update(AgentRunStage)
        .where(AgentRunStage.status == "running", AgentRunStage.started_at < cutoff, AgentRunStage.scan_id.in_(finished))
        .values(status="failed", completed_at=datetime.now(UTC), error=ORPHAN_STAGE_ERROR)
    )
    return result.rowcount or 0
