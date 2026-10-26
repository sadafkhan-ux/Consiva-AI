"""DB-backed job queue (docs/architecture §B) — `SELECT ... FOR UPDATE SKIP LOCKED` so
multiple worker processes can safely share one queue without external infra. Swap for
Celery/arq later if throughput demands it; callers (services/, worker.py) only use the
four functions below, so that swap wouldn't ripple outward."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AgentJob, AgentRunStage
from app.scanner import scan_profiles

_MAX_ATTEMPTS = 3

# Headroom ON TOP OF the longest a job is allowed to legitimately run, not the whole
# allowance. Covers the parts of a job that sit outside the budgeted step -- claiming
# the row, the classification and data-structuring stages, the post-scan diff, the
# final mark_done -- plus clock skew between workers.
_REAP_HEADROOM = timedelta(minutes=10)


def _stale_threshold() -> timedelta:
    """How long a job may hold its row before we treat its worker as dead.

    SELECT...FOR UPDATE SKIP LOCKED stops two live workers taking the same job, but it
    does nothing for a worker that dies mid-job (crash, OOM-kill, host restart) and so
    never calls mark_done/mark_failed. Its row would sit at "running" forever, and the
    work would never happen. Hence reap_stale_jobs.

    The reaper cannot distinguish "dead" from "slow" -- it only sees elapsed time -- so
    this threshold MUST sit above the longest a healthy job may legitimately take.
    It did not. It was hardcoded at 10 minutes, on the reasoning that a real run had
    never taken close to that, while scanner/crawler.py would let a single crawl run
    for Settings.scan_budget_seconds: 870s at the default 25-page limit. Every scan
    between 10 and 14.5 minutes was therefore declared dead while still running, put
    back on the queue, and started again -- a second Chromium crawling the customer's
    site and writing evidence for the same scan, now likelier still because a
    concurrent worker has free slots to pick the duplicate up with. After
    _MAX_ATTEMPTS the job ends up "failed" with a "Reaped" error despite having worked.
    Raising SCANNER_MAX_PAGES makes this the normal case rather than the tail: at 50
    pages the budget is 1620s, so most of a long scan's life would be spent past a
    fixed 10-minute threshold.

    So it is derived. The bound is whichever job type is allowed to run longest:
    "scan" by the crawl budget, "analyze" by the LLM deadline the whole analysis
    (including a validation retry, which reuses the same deadline rather than getting
    a fresh one) is held to. Being too generous only delays recovering a genuinely
    dead job; being too tight duplicates live work against somebody's website, which
    is much worse, so the comparison is deliberately taken over both and padded.
    """
    settings = get_settings()
    longest_legitimate_job = max(settings.scan_budget_seconds, settings.llm_deadline_seconds)
    return timedelta(seconds=longest_legitimate_job) + _REAP_HEADROOM

# Slack added on top of a scan's own hard timeout before the reaper may treat its job as lost.
# The scan aborts itself at that timeout, so this only has to cover the persistence and analysis
# work that follows it in the same job.
_APPLICATION_LEASE_MARGIN = timedelta(minutes=15)


def stale_threshold_for(job: AgentJob) -> timedelta:
    """How long `job` may sit at "running" before the reaper assumes its worker died.

    The derived base threshold (_stale_threshold) for everything that is not a scan. A SCAN may legitimately run longer than that --
    a slow site, or an application scan with no page cap -- so a flat 10 minutes would reclaim a
    healthy scan mid-crawl and start a second crawl of the same site alongside it. A scan job
    therefore gets its profile's own whole-scan ceiling plus a margin (never less than the base): 30 minutes + 15 for a web
    scan, 24 hours + 15 for an application scan. The profile is read the same way scan_service
    reads it: the `scan_profile` the API put in the job payload, else web.
    """
    job_type = getattr(job, "job_type", None)
    if job_type not in ("scan", "consent_api_chain"):
        return _stale_threshold()
    payload = getattr(job, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    name = scan_profiles.APPLICATION if payload.get("scan_profile") == scan_profiles.APPLICATION else scan_profiles.WEB
    profile = scan_profiles.resolve_profile(name)
    lease = timedelta(seconds=profile.hard_timeout_seconds) + _APPLICATION_LEASE_MARGIN
    if job_type == "consent_api_chain":
        # The API's chain job runs the crawl AND then the analysis in one job, so both budgets count: the analysis starts only after the crawl ends.
        lease += timedelta(seconds=get_settings().llm_deadline_seconds)
    return max(_stale_threshold(), lease)


async def enqueue(db: AsyncSession, *, org_id: uuid.UUID, job_type: str, payload: dict) -> AgentJob:
    job = AgentJob(org_id=org_id, job_type=job_type, payload=payload)
    db.add(job)
    await db.flush()
    return job


async def dequeue_batch(db: AsyncSession, *, worker_id: str, limit: int) -> list[AgentJob]:
    """Claim up to `limit` runnable jobs in one round trip, oldest first.

    Same SKIP LOCKED contract as before -- this changes only how many rows a single
    call claims, so neither two worker processes nor two concurrent slots inside one
    process can ever be handed the same job. Claiming N at once rather than looping on
    dequeue_one keeps filling a worker's free slots to one query instead of N.

    The caller must COMMIT before it starts running them: until the status="running"
    write lands, the rows are only row-locked by this transaction, and another worker
    skipping them relies on that lock being real. Holding the transaction open for the
    duration of the work would also pin a pool connection behind every in-flight job.
    """
    if limit <= 0:
        return []
    stmt = (
        select(AgentJob)
        .where(AgentJob.status == "queued", AgentJob.run_after <= datetime.now(UTC))
        .order_by(AgentJob.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list((await db.execute(stmt)).scalars().all())
    claimed_at = datetime.now(UTC)
    for job in jobs:
        job.status = "running"
        job.locked_at = claimed_at
        job.locked_by = worker_id
        job.attempts += 1
    await db.flush()
    return jobs


async def dequeue_one(db: AsyncSession, *, worker_id: str) -> AgentJob | None:
    """Single-job form of dequeue_batch, for callers that want exactly one."""
    jobs = await dequeue_batch(db, worker_id=worker_id, limit=1)
    return jobs[0] if jobs else None


async def mark_done(db: AsyncSession, job_id: uuid.UUID) -> None:
    job = await db.get(AgentJob, job_id)
    if job:
        job.status = "done"
        await db.flush()


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, error: str) -> None:
    job = await db.get(AgentJob, job_id)
    if job is None:
        return
    job.error = error
    if job.attempts >= _MAX_ATTEMPTS:
        job.status = "failed"
    else:
        job.status = "queued"
        job.run_after = datetime.now(UTC) + timedelta(seconds=30 * job.attempts)  # linear backoff
    await db.flush()


async def _close_orphaned_stages(db: AsyncSession, job: AgentJob) -> None:
    """Marks the stage rows a dead worker left at "running" as failed.

    A worker that dies mid-stage never closes its stage row. The job is requeued (or failed), but the row stays
    "running" for ever: a page polling it shows a stage that has been running for hours, and a requeued run adds a
    second row beside it. The reaper is the one place that knows the worker is gone, so it closes them -- keeping the
    time they were observed running, and saying why."""
    payload = getattr(job, "payload", None)
    scan_id = payload.get("scan_id") if isinstance(payload, dict) else None
    if not scan_id:
        return
    try:
        scan_uuid = uuid.UUID(str(scan_id))
    except ValueError:
        return
    now = datetime.now(UTC)
    await db.execute(
        update(AgentRunStage)
        .where(AgentRunStage.scan_id == scan_uuid, AgentRunStage.status == "running")
        .values(
            status="failed", completed_at=now,
            error=f"Interrupted: worker {getattr(job, 'locked_by', None)!r} stopped without finishing this stage",
        )
    )


async def reap_stale_jobs(db: AsyncSession) -> int:
    """Requeues (or fails, if attempts are already exhausted) any job stuck at
    status="running" whose worker evidently died without releasing it. Returns the
    number reaped, for logging. Safe to call from multiple workers concurrently --
    FOR UPDATE SKIP LOCKED means two workers reaping at once split the stale set
    rather than double-touching a row."""
    threshold = _stale_threshold()
    cutoff = datetime.now(UTC) - threshold
    stmt = (
        select(AgentJob)
        .where(AgentJob.status == "running", AgentJob.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    )
    now = datetime.now(UTC)
    # The query above selects everything older than the BASE threshold; a job with a longer
    # lease (an application scan) is only stale once ITS threshold has passed too.
    stale_jobs = [
        j for j in (await db.execute(stmt)).scalars().all()
        if getattr(j, "locked_at", None) is None or j.locked_at < now - stale_threshold_for(j)
    ]
    for job in stale_jobs:
        await _close_orphaned_stages(db, job)
        job.error = f"Reaped: worker {job.locked_by!r} held this job past {stale_threshold_for(job)} without completing it"
        if job.attempts >= _MAX_ATTEMPTS:
            job.status = "failed"
        else:
            job.status = "queued"
            job.run_after = datetime.now(UTC)  # eligible immediately -- this wasn't a normal transient failure
    await db.flush()
    return len(stale_jobs)


async def cancel_jobs_for_scan(db: AsyncSession, *, scan_id: uuid.UUID, org_id: uuid.UUID) -> int:
    """Removes a scan's not-yet-started jobs. Returns how many were removed.

    Only `queued` rows are touched. A job already `running` holds a browser subprocess
    with its own time budget, and marking its row cancelled would not stop that process
    -- it would just make the queue disagree with reality and leave the worker writing
    results for a scan the API has called dead. The scan is marked cancelled by the
    caller either way, and the worker checks that before doing anything further.

    Scoped by org_id as well as scan_id: scan ids are unguessable, but a repository
    function that can touch any tenant's queue rows given one id is the kind of thing
    that becomes a cross-tenant bug the first time a caller forgets to check.
    """
    stmt = (
        select(AgentJob)
        .where(
            AgentJob.org_id == org_id,
            AgentJob.status == "queued",
            AgentJob.payload["scan_id"].astext == str(scan_id),
        )
        .with_for_update(skip_locked=True)
    )
    jobs = (await db.execute(stmt)).scalars().all()
    for job in jobs:
        job.status = "cancelled"
        job.error = "Cancelled via the Consent Agent API before execution"
    await db.flush()
    return len(jobs)
