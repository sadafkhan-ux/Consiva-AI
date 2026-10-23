"""Persistence for llm_usage_events (migration 0041). One insert, idempotent on `call_id`."""

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmUsageEvent


async def insert_event(db: AsyncSession, fields: dict) -> bool:
    """Writes one usage event. Returns False (and writes nothing) when an event with the same `call_id` is already stored, so a repeated callback for the
    same provider call is never counted twice."""
    stmt = pg_insert(LlmUsageEvent).values(**fields).on_conflict_do_nothing(index_elements=["call_id"]).returning(LlmUsageEvent.id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def list_for_scan(db: AsyncSession, scan_id: uuid.UUID) -> list[LlmUsageEvent]:
    from sqlalchemy import select

    rows = await db.execute(select(LlmUsageEvent).where(LlmUsageEvent.scan_id == scan_id).order_by(LlmUsageEvent.created_at))
    return list(rows.scalars().all())
