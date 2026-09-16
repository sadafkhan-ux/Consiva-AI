"""DB-backed job queue (docs/architecture §B) — `SELECT ... FOR UPDATE SKIP LOCKED` so
multiple worker processes can safely share one queue without external infra. Swap for
Celery/arq later if throughput demands it; callers (services/, worker.py) only use the
four functions below, so that swap wouldn't ripple outward."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AgentJob

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
    stale_jobs = (await db.execute(stmt)).scalars().all()
    for job in stale_jobs:
        job.error = f"Reaped: worker {job.locked_by!r} held this job past {threshold} without completing it"
        if job.attempts >= _MAX_ATTEMPTS:
            job.status = "failed"
        else:
            job.status = "queued"
            job.run_after = datetime.now(UTC)  # eligible immediately -- this wasn't a normal transient failure
    await db.flush()
    return len(stale_jobs)
