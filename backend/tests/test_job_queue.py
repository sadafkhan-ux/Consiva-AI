"""Unit tests for jobs/queue.py's reap_stale_jobs -- the real SELECT...FOR UPDATE
SKIP LOCKED + locked_at-cutoff filtering was already verified live against the real
database (a synthetic stale job was inserted, correctly requeued, and left alone on a
second pass with no re-touch; see the reaper audit). These tests lock in the
post-fetch branching logic (requeue vs. permanently fail based on attempts) so a
regression here is caught automatically."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.jobs import queue


def _stale_job(*, attempts):
    return SimpleNamespace(status="running", attempts=attempts, error=None, run_after=None, locked_by="dead-worker")


def _db_returning(jobs):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = jobs
    db.execute = AsyncMock(return_value=result)
    return db


async def test_reap_requeues_job_under_attempt_limit():
    job = _stale_job(attempts=1)
    db = _db_returning([job])

    reaped = await queue.reap_stale_jobs(db)

    assert reaped == 1
    assert job.status == "queued"
    assert job.error is not None and "Reaped" in job.error
    assert job.run_after is not None
    db.flush.assert_awaited_once()


async def test_reap_permanently_fails_job_at_attempt_limit():
    job = _stale_job(attempts=queue._MAX_ATTEMPTS)
    db = _db_returning([job])

    reaped = await queue.reap_stale_jobs(db)

    assert reaped == 1
    assert job.status == "failed"
    assert "Reaped" in job.error


async def test_reap_is_a_noop_when_nothing_is_stale():
    db = _db_returning([])

    reaped = await queue.reap_stale_jobs(db)

    assert reaped == 0
    db.flush.assert_awaited_once()  # still called; harmless no-op on an empty list
