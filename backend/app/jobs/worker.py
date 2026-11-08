"""Polling worker for the agent_jobs queue (docs/architecture §B/§N step 9). Run as a
separate process from the API: `python -m app.jobs.worker`.

Scales two ways, both resting on `SELECT ... FOR UPDATE SKIP LOCKED` in jobs/queue.py:
WORKER_CONCURRENCY slots inside one process, and more than one process against the
same queue. Concurrency lives here rather than in the job bodies because on Linux the
scan path is already a plain coroutine -- run_scan_isolated() only shells out to a
child process on Windows -- so N scans in one event loop really do overlap.

Two independent loops run here, and keeping them apart is the point:

  * the job pump claims work and runs it in up to N slots;
  * the maintenance loop reaps stale jobs and dispatches due monitoring schedules.

These used to be one loop, with the job processed inline. That meant a long job
blocked every periodic duty for its whole duration -- including reap_stale_jobs, the
one mechanism that recovers a hung job, so a wedged job prevented its own recovery and
nothing else ran either (scanner/crawler.py's run_scan_isolated carries the live
incident: one job held the only worker 15+ minutes and five later scans never started).
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Must also run before app.db.session is imported, because that module builds the
# engine at import time.
#
# The worker is deliberately CROSS-TENANT: stale-job reaping walks every organisation's
# rows and the monitoring dispatcher walks every schedule, and there is no single
# org_id to scope them to. Once the API moves to the least-privilege role that
# migration 0018 creates, row-level security would correctly show this process
# nothing -- and the sweeps would stop silently, which is the worst way for them to
# stop. So the worker takes its own connection string when one is given.
#
# The .env load below is not redundant. This block runs BEFORE app.config is imported,
# and app.config is what reads .env -- so without this, a WORKER_DATABASE_URL that
# lives in the file rather than in the real environment was invisible here, and the
# worker quietly ran on the restricted role with every sweep returning nothing. That
# is the exact silent failure the paragraph above warns about, reintroduced by the
# order of two imports. `override=False` keeps a real environment variable (compose,
# systemd) winning over the file.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

if os.getenv("WORKER_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["WORKER_DATABASE_URL"]

# Must run before any event loop is created (see scanner/run_single_scan.py for the
# full explanation): the LangGraph checkpointer's psycopg needs SelectorEventLoop on
# Windows; Playwright needs Proactor. This process handles "analyze" jobs (checkpointer,
# needs Selector) directly, and dispatches "scan" jobs (Playwright) to an isolated child
# process via scanner/crawler.py's run_scan_isolated(), which sidesteps the conflict
# with plain (non-asyncio) subprocess.run rather than needing Proactor itself.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import get_settings
from app.db.models import AgentJob
from app.db.session import async_session_factory
from app.jobs import capacity, queue
from app.services import (
    analysis_service,
    consent_api_chain_service,
    monitoring_service,
    report_cache_service,
    scan_service,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How long an idle worker waits before looking for queued work again. This is the floor on the hand-off between
# the steps of one scan (the console enqueues the analysis the moment the crawl is seen to finish, and nothing
# runs it until the next poll). Measured on scan ac537cef: ~2.1s from crawl-complete to analysis start. 0.5s costs
# the database two trivially cheap indexed SELECTs per second per idle worker (status, run_after index).
POLL_INTERVAL_SECONDS = 0.5
# The maintenance loop's own cadence, now that it no longer shares the job loop. 2s was
# never a requirement of the work -- a monitoring interval is measured in hours --
# it was just whatever the job poll happened to be. 30s also keeps the
# cross-tenant queries off the database 15x less often, which matters more now
# that several workers may each be running them.
MAINTENANCE_INTERVAL_SECONDS = 30
# How often the worker logs what it is doing about capacity (running, CPU, memory, why it is holding), so an operator can see why jobs are waiting.
CAPACITY_REPORT_SECONDS = 30
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
    elif job.job_type == "consent_api_chain":
        # One API-requested scan, end to end: crawl, analyse, notify. The console's
        # two-step flow still uses the separate "scan" and "analyze" types above and is
        # untouched -- see services/consent_api_chain_service for why an integrator
        # needs the phases joined and a person does not.
        await consent_api_chain_service.run(
            uuid.UUID(job.payload["scan_id"]),
            uuid.UUID(job.payload["org_id"]),
            uuid.UUID(job.payload["user_id"]),
        )
    elif job.job_type == "consent_webhook":
        await consent_api_chain_service.deliver_webhook(job.payload)
    elif job.job_type == "analyze":
        await analysis_service.run_analysis(
            uuid.UUID(job.payload["agent_run_id"]),
            uuid.UUID(job.payload["scan_id"]),
            uuid.UUID(job.payload["org_id"]),
        )
    elif job.job_type == "render_reports":
        # Pre-generation: print and keep a finished scan's reports so the first download is already waiting (services/report_cache_service.py).
        await report_cache_service.prewarm(uuid.UUID(job.payload["scan_id"]))
    else:
        raise ValueError(f"Unknown job_type: {job.job_type}")


async def _sleep_or_stop(stopping: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake the moment shutdown is requested, so SIGTERM doesn't have to
    wait out a full interval before anything responds to it."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


async def _run_job(job: AgentJob) -> None:
    """One job start to finish, including recording its own outcome.

    Every exception is handled in here. This runs as a detached task, so anything that
    escaped would surface only as an "exception was never retrieved" warning at garbage
    collection time, and the job row would sit at status="running" until the reaper
    noticed it ten minutes later.
    """
    logger.info("Processing job %s (%s)", job.id, job.job_type)
    try:
        await _process_one(job)
    except Exception as exc:  # a bad job must not kill the worker loop
        logger.exception("Job %s (%s) failed", job.id, job.job_type)
        async with async_session_factory() as db:
            # A failure that running the job again cannot cure (ScanNotPossible) is not retried.
            await queue.mark_failed(db, job.id, str(exc), retryable=getattr(exc, "retryable", True))
            await db.commit()
    else:
        async with async_session_factory() as db:
            await queue.mark_done(db, job.id)
            await db.commit()


async def _maintenance_loop(stopping: asyncio.Event) -> None:
    """Stale-job reaping and the monitoring dispatcher.

    Every step is wrapped. On the old shared loop, reaping and dispatch were bare, so a
    failure there killed the process and `restart: unless-stopped` brought it back --
    crude, but it did not fail silently. On a dedicated task an unhandled exception
    would instead kill just this task and leave the pump running happily, which is
    exactly the kind of silent stop nobody would notice. So each step
    catches, logs and continues, and the loop itself is the only thing that can end.
    """
    while not stopping.is_set():
        try:
            async with async_session_factory() as db:
                reaped = await queue.reap_stale_jobs(db)
                await db.commit()
            if reaped:
                logger.warning("Reaped %d stale job(s) whose worker never released them", reaped)
        except Exception:
            logger.exception("Stale-job reaping failed; continuing with the other duties")

        try:
            async with async_session_factory() as db:
                dispatched = await monitoring_service.dispatch_due_schedules(db)
            if dispatched:
                logger.info("Monitoring: dispatched %d due scheduled re-scan(s)", dispatched)
        except Exception:
            logger.exception("Monitoring dispatch failed; continuing with the other duties")


        await _sleep_or_stop(stopping, MAINTENANCE_INTERVAL_SECONDS)


async def _job_pump(stopping: asyncio.Event, governor: capacity.Governor) -> None:
    """Keep as many jobs running as the machine can take right now (app/jobs/capacity.py), and never await one inline."""
    if isinstance(governor, int):
        governor = capacity.FixedSlots(governor)
    in_flight: set[asyncio.Task] = set()
    last_report = 0.0

    while not stopping.is_set():
        claimed: list[AgentJob] = []
        free = governor.claimable(len(in_flight))
        if time.monotonic() - last_report >= CAPACITY_REPORT_SECONDS:
            last_report = time.monotonic()
            cpu, mem = governor.load.cpu_busy_percent(), governor.load.memory_available_mb()
            logger.info("Capacity: %d of up to %d jobs running; CPU %s, memory available %s; %s", len(in_flight), governor.slots,
                        f"{cpu:.0f}%" if cpu is not None else "unknown", f"{mem:.0f} MB" if mem is not None else "unknown", governor.last_reason)
        if free > 0:
            try:
                async with async_session_factory() as db:
                    claimed = await queue.dequeue_batch(db, worker_id=WORKER_ID, limit=free)
                    # Commit before running anything: the claim has to be durable, not
                    # just row-locked, before this transaction's connection goes back
                    # to the pool. See dequeue_batch's docstring.
                    await db.commit()
            except Exception:
                logger.exception("Could not claim jobs; retrying after the poll interval")

        for job in claimed:
            task = asyncio.create_task(_run_job(job), name=f"job-{job.id}")
            in_flight.add(task)
            # discard, not remove: the shutdown drain below may already have taken it.
            task.add_done_callback(in_flight.discard)

        if claimed:
            continue  # slots may still be free, and more work may be queued -- refill now

        if in_flight:
            # Every slot is busy. Wake on the first completion rather than sleeping a
            # fixed interval, so a freed slot is refilled immediately; the timeout is
            # what still bounds the wait when nothing finishes.
            await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED, timeout=POLL_INTERVAL_SECONDS)
        else:
            await _sleep_or_stop(stopping, POLL_INTERVAL_SECONDS)

    if in_flight:
        # Shutting down: stop claiming, but let what is already running finish and
        # record its own outcome. Anything still going when the container's
        # stop_grace_period expires is SIGKILLed and recovered by reap_stale_jobs --
        # correct, but ten minutes later, so the grace period is set in
        # docker-compose.prod.yml to cover a realistic scan instead.
        logger.info("Shutting down: waiting for %d in-flight job(s)", len(in_flight))
        await asyncio.gather(*in_flight, return_exceptions=True)


async def run_forever() -> None:
    settings = get_settings()
    governor = capacity.governor_for(settings)
    logger.info("Worker %s starting: up to %d jobs at once (%s), taking more only while CPU is under %d%% and memory remains", WORKER_ID, governor.slots,
                "set by WORKER_CONCURRENCY" if settings.worker_concurrency > 0 else "from the CPU count", settings.worker_target_cpu_percent)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows' ProactorEventLoop; there the process is stopped
        # the blunt way and reap_stale_jobs cleans up, same as before.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    tasks = {
        asyncio.create_task(_maintenance_loop(stopping), name="maintenance"),
        asyncio.create_task(_job_pump(stopping, governor), name="job-pump"),
    }

    # If either loop ends -- shutdown signal, or a bug that got past its own handlers
    # -- bring the other down too and let the process exit, so the restart policy is
    # what decides what happens next. A worker running with half its duties silently
    # missing is the failure mode worth avoiding.
    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stopping.set()
    for task in done:
        # .exception() re-raises on a cancelled task, so a Ctrl-C landing between the
        # wait and this line must not turn shutdown reporting into its own crash.
        if task.cancelled():
            continue
        if (exc := task.exception()) is not None:
            logger.error("Worker loop %r exited with an exception", task.get_name(), exc_info=exc)
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Worker %s stopped", WORKER_ID)


if __name__ == "__main__":
    asyncio.run(run_forever())
