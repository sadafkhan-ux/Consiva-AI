"""DB-backed job queue (docs/architecture §B) — `SELECT ... FOR UPDATE SKIP LOCKED` so
multiple worker processes can safely share one queue without external infra. Swap for
Celery/arq later if throughput demands it; callers (services/, worker.py) only use the
four functions below, so that swap wouldn't ripple outward."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentJob
from app.scanner import scan_profiles

_MAX_ATTEMPTS = 3

# Generous on purpose: the longest real pipeline run observed (scan + analyze, including
# a validation retry) is well under 3 minutes. A job still "running" past this has
# almost certainly lost its worker (crash, OOM-kill, host restart) rather than still
# being legitimately in progress -- SELECT...FOR UPDATE SKIP LOCKED prevents two live
# workers from double-processing the same job, but does nothing for one that dies
# mid-job and never calls mark_done/mark_failed to release it.
_STALE_THRESHOLD = timedelta(minutes=10)

# Slack added on top of an application scan's own hard timeout before the reaper may treat its
# job as lost. The scan aborts itself at that timeout, so this only has to cover the persistence
# and analysis work that follows it in the same job.
_APPLICATION_LEASE_MARGIN = timedelta(minutes=15)


def stale_threshold_for(job: AgentJob) -> timedelta:
    """How long `job` may sit at "running" before the reaper assumes its worker died.

    10 minutes for everything, which is generous for a web scan (a few pages, bounded by its own
    timeout). An APPLICATION scan has no page cap and is allowed to run for CONSENT_APP_MAX_SECONDS
    -- up to hours -- so a flat 10 minutes would reclaim a healthy scan mid-crawl and start a
    second crawl of the same site alongside it. Such a job (marked by the `scan_profile` the API
    puts in its payload) gets the profile's own hard timeout plus a margin instead. Everything
    else, including jobs created before profiles existed, keeps the 10 minutes.
    """
    payload = getattr(job, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("scan_profile") == scan_profiles.APPLICATION:
        profile = scan_profiles.resolve_profile(scan_profiles.APPLICATION)
        return max(_STALE_THRESHOLD, timedelta(seconds=profile.hard_timeout_seconds) + _APPLICATION_LEASE_MARGIN)
    return _STALE_THRESHOLD


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
    cutoff = datetime.now(UTC) - _STALE_THRESHOLD
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
