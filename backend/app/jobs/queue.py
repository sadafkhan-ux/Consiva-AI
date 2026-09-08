"""DB-backed job queue (docs/architecture §B) — `SELECT ... FOR UPDATE SKIP LOCKED` so
multiple worker processes can safely share one queue without external infra. Swap for
Celery/arq later if throughput demands it; callers (services/, worker.py) only use the
four functions below, so that swap wouldn't ripple outward."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentJob

_MAX_ATTEMPTS = 3

# Generous on purpose: the longest real pipeline run observed (scan + analyze, including
# a validation retry) is well under 3 minutes. A job still "running" past this has
# almost certainly lost its worker (crash, OOM-kill, host restart) rather than still
# being legitimately in progress -- SELECT...FOR UPDATE SKIP LOCKED prevents two live
# workers from double-processing the same job, but does nothing for one that dies
# mid-job and never calls mark_done/mark_failed to release it.
_STALE_THRESHOLD = timedelta(minutes=10)


async def enqueue(db: AsyncSession, *, org_id: uuid.UUID, job_type: str, payload: dict) -> AgentJob:
    job = AgentJob(org_id=org_id, job_type=job_type, payload=payload)
    db.add(job)
    await db.flush()
    return job


async def dequeue_one(db: AsyncSession, *, worker_id: str) -> AgentJob | None:
    stmt = (
        select(AgentJob)
        .where(AgentJob.status == "queued", AgentJob.run_after <= datetime.now(UTC))
        .order_by(AgentJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.locked_at = datetime.now(UTC)
    job.locked_by = worker_id
    job.attempts += 1
    await db.flush()
    return job


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
    stale_jobs = (await db.execute(stmt)).scalars().all()
    for job in stale_jobs:
        job.error = f"Reaped: worker {job.locked_by!r} held this job past {_STALE_THRESHOLD} without completing it"
        if job.attempts >= _MAX_ATTEMPTS:
            job.status = "failed"
        else:
            job.status = "queued"
            job.run_after = datetime.now(UTC)  # eligible immediately -- this wasn't a normal transient failure
    await db.flush()
    return len(stale_jobs)
