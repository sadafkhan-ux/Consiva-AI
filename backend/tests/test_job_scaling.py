"""Job retries and fair scheduling.

A scan or analysis that fails is retried with a wait that doubles (30 s, 1, 2, 4 ... at most 15 min) instead of 30 s then 60 s, and is given more attempts, so
an outage of half an hour no longer loses the job. A job whose failure running it again cannot cure (a site that refuses the scanner) is not run again at all.
With many organisations submitting at once, the queue takes turns between them instead of serving whoever submitted first until they are done."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.jobs import queue, worker
from app.scanner.failures import ScanNotPossible


def job(attempts):
    return SimpleNamespace(id=uuid.uuid4(), attempts=attempts, status="running", error=None, run_after=None)


def db_with(j):
    db = MagicMock()
    db.get = AsyncMock(return_value=j)
    db.flush = AsyncMock()
    return db


class TestRetrySpacing:
    @pytest.mark.parametrize("attempts,low", [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 900), (12, 900)])
    def test_the_wait_doubles_then_stops_at_the_ceiling(self, attempts, low):
        for _ in range(20):
            d = queue.retry_delay_seconds(attempts)
            assert low <= d <= low * 1.1 + 0.001

    def test_more_attempts_are_allowed_than_the_three_that_used_to_end_a_job_after_about_two_minutes(self):
        assert queue._MAX_ATTEMPTS >= 6
        total = sum(queue.retry_delay_seconds(n) for n in range(1, queue._MAX_ATTEMPTS))
        assert total >= 14 * 60, "a job should survive an outage of about a quarter of an hour (it used to be about 90 seconds)"

    async def test_a_failed_attempt_is_requeued_with_the_growing_wait_until_the_attempts_run_out(self):
        for attempts in range(1, queue._MAX_ATTEMPTS):
            j = job(attempts)
            await queue.mark_failed(db_with(j), j.id, "boom")
            assert j.status == "queued" and j.run_after > datetime.now(UTC) and j.error == "boom"
        last = job(queue._MAX_ATTEMPTS)
        await queue.mark_failed(db_with(last), last.id, "boom")
        assert last.status == "failed"

    async def test_a_failure_that_running_it_again_cannot_cure_is_left_failed_at_once(self):
        j = job(1)
        await queue.mark_failed(db_with(j), j.id, "The site refused the scanner", retryable=False)
        assert j.status == "failed" and j.error == "The site refused the scanner"

    async def test_a_missing_job_is_ignored(self):
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        await queue.mark_failed(db, uuid.uuid4(), "x")


class TestTheWorkerPassesTheClassification:
    async def _run(self, monkeypatch, exc):
        recorded = {}

        async def process(_job):
            raise exc

        async def mark_failed(_db, job_id, error, *, retryable=True):
            recorded.update(error=error, retryable=retryable)

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def commit(self):
                return None

        monkeypatch.setattr(worker, "_process_one", process)
        monkeypatch.setattr(worker.queue, "mark_failed", mark_failed)
        monkeypatch.setattr(worker, "async_session_factory", lambda: Session())
        await worker._run_job(SimpleNamespace(id=uuid.uuid4(), job_type="scan"))
        return recorded

    async def test_an_impossible_scan_is_not_retried(self, monkeypatch):
        r = await self._run(monkeypatch, ScanNotPossible("Could not reach the site at all: refused", "refused"))
        assert r["retryable"] is False and "Could not reach the site at all" in r["error"]

    async def test_any_other_failure_still_is(self, monkeypatch):
        assert (await self._run(monkeypatch, RuntimeError("database restarted")))["retryable"] is True
        assert (await self._run(monkeypatch, TimeoutError("slow")))["retryable"] is True


class TestFairScheduling:
    def sql(self, limit=4):
        return str(queue._fair_candidates(limit).compile(dialect=postgresql.dialect()))

    def test_each_job_gets_a_turn_number_from_its_place_in_its_own_organisations_queue_plus_what_that_organisation_already_runs(self):
        text = self.sql()
        assert "row_number() OVER (PARTITION BY agent_jobs.org_id ORDER BY agent_jobs.created_at, agent_jobs.id)" in text
        assert "agent_jobs_1.org_id = agent_jobs.org_id AND agent_jobs_1.status = " in text
        assert "ORDER BY row_number() OVER" in text and "LIMIT" in text

    def test_only_runnable_queued_jobs_are_candidates(self):
        text = self.sql()
        assert "agent_jobs.status = %(status_1)s" in text and "agent_jobs.run_after <=" in text

    def test_the_claim_still_locks_with_skip_locked_so_workers_never_share_a_job(self):
        import inspect
        src = inspect.getsource(queue.dequeue_batch)
        assert "skip_locked=True" in src and "_fair_candidates(limit)" in src and 'AgentJob.status == "queued"' in src

    async def test_asking_for_nothing_claims_nothing(self):
        db = MagicMock()
        db.execute = AsyncMock()
        assert await queue.dequeue_batch(db, worker_id="w", limit=0) == []
        db.execute.assert_not_awaited()
