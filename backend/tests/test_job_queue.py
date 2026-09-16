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


# ── dequeue_batch: claiming several jobs at once ─────────────────────────────────
# The SKIP LOCKED clause itself is verified live (see the module docstring); these
# lock in the claim-stamping and the limit handling, which is what changed when the
# worker gained concurrent slots and started asking for more than one job at a time.

def _queued_job():
    return SimpleNamespace(status="queued", attempts=0, locked_at=None, locked_by=None)


async def test_dequeue_batch_stamps_every_job_it_claims():
    jobs = [_queued_job() for _ in range(3)]
    db = _db_returning(jobs)

    claimed = await queue.dequeue_batch(db, worker_id="w-1", limit=3)

    assert len(claimed) == 3
    for job in claimed:
        assert job.status == "running"
        assert job.locked_by == "w-1"
        assert job.locked_at is not None
        assert job.attempts == 1
    db.flush.assert_awaited_once()


async def test_dequeue_batch_does_not_query_when_there_are_no_free_slots():
    """A full worker asks for zero jobs. That has to be free, not a query returning
    rows the worker would then have to put back -- the rows would already be stamped
    status="running" by the time it noticed."""
    db = _db_returning([_queued_job()])

    assert await queue.dequeue_batch(db, worker_id="w-1", limit=0) == []
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_dequeue_one_is_the_single_job_form_of_dequeue_batch():
    job = _queued_job()
    db = _db_returning([job])

    claimed = await queue.dequeue_one(db, worker_id="w-1")

    assert claimed is job
    assert job.status == "running"


async def test_dequeue_one_returns_none_on_an_empty_queue():
    assert await queue.dequeue_one(_db_returning([]), worker_id="w-1") is None
