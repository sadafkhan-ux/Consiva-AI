"""Polling worker for the agent_jobs queue (docs/architecture §B/§N step 9). Run as a
separate process from the API: `python -m app.jobs.worker`. Scale by running more than
one — `SELECT ... FOR UPDATE SKIP LOCKED` in jobs/queue.py makes that safe.
"""

import asyncio
import logging
import socket
import sys
import uuid

# Must run before any event loop is created (see scanner/run_single_scan.py for the
# full explanation): the LangGraph checkpointer's psycopg needs SelectorEventLoop on
# Windows; Playwright needs Proactor. This process handles "analyze" jobs (checkpointer,
# needs Selector) directly, and dispatches "scan" jobs (Playwright) to an isolated child
# process via scanner/crawler.py's run_scan_isolated(), which sidesteps the conflict
# with plain (non-asyncio) subprocess.run rather than needing Proactor itself.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db.models import AgentJob
from app.db.session import async_session_factory
from app.jobs import queue
from app.services import analysis_service, monitoring_service, scan_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def _process_one(job: AgentJob) -> None:
    if job.job_type == "scan":
        scan_id = uuid.UUID(job.payload["scan_id"])
        await scan_service.execute_scan_and_persist(scan_id, job.payload["url"])
        # Continuous Monitoring (Build Plan Component 9): a no-op for a manually-
        # requested scan (nothing references it as schedule-triggered); for a
        # scheduled one, diffs against baseline and re-enters analysis on material
        # change. Runs only after a SUCCESSFUL scan -- execute_scan_and_persist raises
        # on failure, which skips this line entirely, same as any other job failure.
        await monitoring_service.maybe_run_diff_after_scan(scan_id)
    elif job.job_type == "analyze":
        await analysis_service.run_analysis(
            uuid.UUID(job.payload["agent_run_id"]),
            uuid.UUID(job.payload["scan_id"]),
            uuid.UUID(job.payload["org_id"]),
        )
    else:
        raise ValueError(f"Unknown job_type: {job.job_type}")


async def run_forever() -> None:
    logger.info("Worker %s starting", WORKER_ID)
    while True:
        async with async_session_factory() as db:
            reaped = await queue.reap_stale_jobs(db)
            await db.commit()
        if reaped:
            logger.warning("Reaped %d stale job(s) whose worker never released them", reaped)

        async with async_session_factory() as db:
            dispatched = await monitoring_service.dispatch_due_schedules(db)
        if dispatched:
            logger.info("Monitoring: dispatched %d due scheduled re-scan(s)", dispatched)

        async with async_session_factory() as db:
            job = await queue.dequeue_one(db, worker_id=WORKER_ID)
            await db.commit()

        if job is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        logger.info("Processing job %s (%s)", job.id, job.job_type)
        try:
            await _process_one(job)
        except Exception as exc:  # a bad job must not kill the worker loop
            logger.exception("Job %s (%s) failed", job.id, job.job_type)
            async with async_session_factory() as db:
                await queue.mark_failed(db, job.id, str(exc))
                await db.commit()
        else:
            async with async_session_factory() as db:
                await queue.mark_done(db, job.id)
                await db.commit()


if __name__ == "__main__":
    asyncio.run(run_forever())
