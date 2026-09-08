"""Continuous Monitoring (Build Plan Component 9): scheduling due re-scans and, once a
scheduled scan completes, diffing it against the approved baseline and re-entering the
flow (new Action + a real analysis run) when the diff is material — "nothing changes
silently" (master reference §3/§11)."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.repositories import monitoring_repository, scan_repository
from app.db.session import async_session_factory
from app.jobs import queue
from app.services import analysis_service, audit_service
from app.services.diff_engine import diff_evidence

logger = logging.getLogger(__name__)

# Distinct, non-colliding sentinel actor for system/schedule-triggered actions —
# never a real user's id, so it's unambiguous in the audit log which decisions were
# a human's and which were the scheduler's own (master reference: "fail loudly / no
# silent gap" applies to attribution too, not just to errors).
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


async def set_schedule(
    db: AsyncSession, *, org_id: uuid.UUID, website_id: uuid.UUID, interval_hours: int, enabled: bool,
):
    """Sets up (or updates) recurring monitoring for a website. If the website has no
    baseline yet, the NEXT scheduled scan becomes the baseline automatically (there's
    nothing to diff against yet); an existing baseline is left untouched here — only a
    reviewer explicitly promoting a scan (promote_baseline) changes it thereafter.

    Confirmed live by a real cross-org test: without this ownership check, a
    website_id belonging to a DIFFERENT org either (a) let that org silently update
    the real owner's schedule (upsert_schedule's own lookup had no org filter -- fixed
    separately, defense in depth), or (b) for a website_id owned by nobody at all,
    caused a raw Postgres ForeignKeyViolationError to leak as an unhandled 500 instead
    of a clean 404. Checking ownership here, before either of those code paths runs,
    closes both at the source."""
    website = await scan_repository.get_website_by_id(db, website_id)
    if website is None or website.org_id != org_id:
        raise NotFoundError(f"Website {website_id} not found")

    existing = await monitoring_repository.get_schedule_for_website(db, website_id, org_id)
    baseline = existing.baseline_scan_id if existing else None
    return await monitoring_repository.upsert_schedule(
        db, org_id=org_id, website_id=website_id, interval_hours=interval_hours, enabled=enabled,
        baseline_scan_id=baseline,
    )


async def promote_baseline(db: AsyncSession, *, schedule_id: uuid.UUID, org_id: uuid.UUID, scan_id: uuid.UUID):
    schedule = await monitoring_repository.get_schedule_by_id(db, schedule_id, org_id)
    if schedule is None:
        return None
    schedule.baseline_scan_id = scan_id
    schedule.updated_at = datetime.now(UTC)
    await db.flush()
    return schedule


async def dispatch_due_schedules(db: AsyncSession) -> int:
    """Called once per worker loop iteration (alongside reap_stale_jobs) — enqueues a
    real "scan" job (the SAME job type/path a manually-requested scan uses) for every
    website whose schedule is due, and bumps next_run_at so it isn't re-fired next
    iteration. Returns the number dispatched, for logging."""
    due = await monitoring_repository.due_schedules(db)
    for schedule in due:
        website = await scan_repository.get_website_by_id(db, schedule.website_id)
        if website is None:
            continue
        scan = await scan_repository.create_scan(
            db, org_id=schedule.org_id, website_id=website.id, url=f"https://{website.domain}",
            authorized_by_user_id=SYSTEM_ACTOR_ID,
        )
        await audit_service.record(
            db, org_id=schedule.org_id, actor_user_id=SYSTEM_ACTOR_ID, action="scan.created",
            entity_type="consent_scan", entity_id=scan.id,
            after={"triggered_by": "monitoring_schedule", "schedule_id": str(schedule.id)},
        )
        await queue.enqueue(db, org_id=schedule.org_id, job_type="scan", payload={"scan_id": str(scan.id), "url": scan.url})
        await monitoring_repository.mark_dispatched(db, schedule, triggered_scan_id=scan.id)
    if due:
        await db.commit()
    return len(due)


async def maybe_run_diff_after_scan(scan_id: uuid.UUID) -> None:
    """Called by the worker right after a "scan" job completes successfully. A no-op
    for a manually-requested scan (no schedule references it). For a schedule-
    triggered scan: establishes the baseline if this is the schedule's first scan,
    otherwise diffs against the current baseline, persists the diff, and — only when
    the diff is material — creates a review Action and triggers a real analysis run
    (findings re-enter human review exactly like any other scan's would)."""
    async with async_session_factory() as db:
        schedule = await monitoring_repository.schedule_by_triggered_scan(db, scan_id)
        if schedule is None:
            return  # not a schedule-triggered scan; nothing to do

        if schedule.baseline_scan_id is None:
            schedule.baseline_scan_id = scan_id
            await db.commit()
            logger.info("Monitoring: scan %s established as the new baseline for schedule %s", scan_id, schedule.id)
            return

        if schedule.baseline_scan_id == scan_id:
            return  # this scan IS the baseline (e.g. just promoted); nothing to diff

        baseline_evidence = await scan_repository.get_scan_evidence_summary_concurrent(schedule.baseline_scan_id)
        new_evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id)
        result = diff_evidence(baseline_evidence, new_evidence)

        diff = await monitoring_repository.create_diff(
            db, org_id=schedule.org_id, website_id=schedule.website_id,
            baseline_scan_id=schedule.baseline_scan_id, new_scan_id=scan_id,
            added=result["added"], removed=result["removed"], changed=result["changed"],
            has_material_change=result["has_material_change"],
        )
        await audit_service.record(
            db, org_id=schedule.org_id, actor_user_id=SYSTEM_ACTOR_ID, action="scan.diffed",
            entity_type="consent_scan", entity_id=scan_id,
            after={"diff_id": str(diff.id), "has_material_change": result["has_material_change"]},
        )

        if not result["has_material_change"]:
            await db.commit()
            logger.info("Monitoring: scan %s diffed against baseline, no material change", scan_id)
            return

        # Findings re-enter the flow exactly like any other scan's would (master
        # reference §3: "any material change re-enters the flow at analysis and goes
        # back through human review") -- reuses the SAME trigger_analysis() a manual
        # "Analyze" button call goes through, not a parallel code path.
        agent_run = await analysis_service.trigger_analysis(
            db, scan_id=scan_id, org_id=schedule.org_id, user_id=SYSTEM_ACTOR_ID
        )
        logger.info(
            "Monitoring: material change detected for scan %s (diff %s) -- analysis %s enqueued",
            scan_id, diff.id, agent_run.id,
        )
