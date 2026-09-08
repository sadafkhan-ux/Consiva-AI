import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScanDiff, ScanSchedule


async def upsert_schedule(
    db: AsyncSession, *, org_id: uuid.UUID, website_id: uuid.UUID, interval_hours: int, enabled: bool,
    baseline_scan_id: uuid.UUID | None,
) -> ScanSchedule:
    # Scoped by org_id defensively, matching every other org-scoped write in this
    # codebase -- confirmed via a real cross-org test that a website_id-only lookup
    # here let a DIFFERENT org silently update another org's real schedule (its own
    # interval_hours/enabled), even though the caller (set_schedule) already checks
    # website ownership before calling this. Two independent checks, not one.
    result = await db.execute(
        select(ScanSchedule).where(ScanSchedule.website_id == website_id, ScanSchedule.org_id == org_id)
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        schedule = ScanSchedule(
            org_id=org_id, website_id=website_id, interval_hours=interval_hours, enabled=enabled,
            baseline_scan_id=baseline_scan_id, next_run_at=datetime.now(UTC) + timedelta(hours=interval_hours),
        )
        db.add(schedule)
    else:
        schedule.interval_hours = interval_hours
        schedule.enabled = enabled
        if baseline_scan_id is not None:
            schedule.baseline_scan_id = baseline_scan_id
        schedule.updated_at = datetime.now(UTC)
    await db.flush()
    return schedule


async def get_schedule_for_website(db: AsyncSession, website_id: uuid.UUID, org_id: uuid.UUID) -> ScanSchedule | None:
    result = await db.execute(
        select(ScanSchedule).where(ScanSchedule.website_id == website_id, ScanSchedule.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def get_schedule_by_id(db: AsyncSession, schedule_id: uuid.UUID, org_id: uuid.UUID) -> ScanSchedule | None:
    result = await db.execute(
        select(ScanSchedule).where(ScanSchedule.id == schedule_id, ScanSchedule.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def due_schedules(db: AsyncSession) -> list[ScanSchedule]:
    result = await db.execute(
        select(ScanSchedule).where(ScanSchedule.enabled.is_(True), ScanSchedule.next_run_at <= datetime.now(UTC))
    )
    return list(result.scalars().all())


async def schedule_by_triggered_scan(db: AsyncSession, scan_id: uuid.UUID) -> ScanSchedule | None:
    result = await db.execute(select(ScanSchedule).where(ScanSchedule.last_triggered_scan_id == scan_id))
    return result.scalar_one_or_none()


async def mark_dispatched(db: AsyncSession, schedule: ScanSchedule, *, triggered_scan_id: uuid.UUID) -> None:
    """Bumps next_run_at forward immediately at dispatch time (not after the scan
    completes) so the due-schedules poll can't re-fire the same schedule while a scan
    is still in flight -- a documented simplification: if a scan legitimately takes
    longer than the interval, the next cycle still fires on schedule rather than
    drifting, at the cost of possibly overlapping with a still-running previous scan."""
    schedule.last_triggered_scan_id = triggered_scan_id
    schedule.next_run_at = datetime.now(UTC) + timedelta(hours=schedule.interval_hours)
    schedule.updated_at = datetime.now(UTC)
    await db.flush()


async def create_diff(
    db: AsyncSession, *, org_id: uuid.UUID, website_id: uuid.UUID, baseline_scan_id: uuid.UUID,
    new_scan_id: uuid.UUID, added: dict, removed: dict, changed: dict, has_material_change: bool,
) -> ScanDiff:
    diff = ScanDiff(
        org_id=org_id, website_id=website_id, baseline_scan_id=baseline_scan_id, new_scan_id=new_scan_id,
        added=added, removed=removed, changed=changed, has_material_change=has_material_change,
    )
    db.add(diff)
    await db.flush()
    return diff


async def list_diffs_for_website(db: AsyncSession, website_id: uuid.UUID, org_id: uuid.UUID) -> list[ScanDiff]:
    result = await db.execute(
        select(ScanDiff)
        .where(ScanDiff.website_id == website_id, ScanDiff.org_id == org_id)
        .order_by(ScanDiff.created_at.desc())
    )
    return list(result.scalars().all())
