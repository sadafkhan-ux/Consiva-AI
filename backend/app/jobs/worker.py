"""Polling worker for the agent_jobs queue (docs/architecture §B/§N step 9). Run as a
separate process from the API: `python -m app.jobs.worker`.

Scales two ways, both resting on `SELECT ... FOR UPDATE SKIP LOCKED` in jobs/queue.py:
WORKER_CONCURRENCY slots inside one process, and more than one process against the
same queue. Concurrency lives here rather than in the job bodies because on Linux the
scan path is already a plain coroutine -- run_scan_isolated() only shells out to a
child process on Windows -- so N scans in one event loop really do overlap.

Two independent loops run here, and keeping them apart is the point:

  * the job pump claims work and runs it in up to N slots;
  * the maintenance loop reaps stale jobs, dispatches due monitoring schedules and
    runs the two SLA sweeps.

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
import uuid

# Must also run before app.db.session is imported, because that module builds the
# engine at import time.
#
# The worker is deliberately CROSS-TENANT: the SLA sweeps walk every organisation's
# rows and the monitoring dispatcher walks every schedule, and there is no single
# org_id to scope them to. Once the API moves to the least-privilege role that
# migration 0018 creates, row-level security would correctly show this process
# nothing -- and the sweeps would stop silently, which is the worst way for them to
# stop. So the worker takes its own connection string when one is given.
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

from app.agents.breach.services import sla_service as incident_sla_service
from app.agents.dsr.services import sla_service as dsr_sla_service
from app.agents.regwatch.services import action_sla_service as regwatch_action_sla_service
from app.config import get_settings
from app.db.models import AgentJob
from app.db.session import async_session_factory
from app.jobs import queue
from app.services import (
    analysis_service,
    dsr_run_service,
    incident_run_service,
    monitoring_service,
    regwatch_run_service,
    ropa_run_service,
    scan_service,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
# The maintenance loop's own cadence, now that it no longer shares the job loop. 2s was
# never a requirement of the work -- a DSR deadline is measured in days and a monitoring
# interval in hours -- it was just whatever the job poll happened to be. 30s also keeps
# three cross-tenant queries off the database 15x less often, which matters more now
# that several workers may each be running them.
MAINTENANCE_INTERVAL_SECONDS = 30
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
    elif job.job_type == "ropa_discovery":
        # Agent 2 (Data Discovery / ROPA) connector run. Only the connector path
        # is queued: an evidence_push already arrives with its data in hand and
        # completes inside the request, so queuing it would add latency for
        # nothing. Uses this same agent_jobs queue rather than a second job
        # framework.
        await ropa_run_service.execute_queued_discovery(
            uuid.UUID(job.payload["run_id"]),
            uuid.UUID(job.payload["org_id"]),
        )
    elif job.job_type == "dsr_search":
        # Agent 3 (DSR Fulfillment) subject search across authorized sources. Uses
        # this same agent_jobs queue rather than a third job framework -- a DSR
        # search can take as long as the slowest source, which is exactly the kind
        # of work that must not run inside a request.
        await dsr_run_service.execute_queued_search(
            uuid.UUID(job.payload["request_id"]),
            uuid.UUID(job.payload["org_id"]),
            job_id=job.id,
        )
    elif job.job_type == "dsr_execute":
        # Executes the APPROVED actions on a DSR case. Safe to retry: every action
        # claims an idempotency key before it does anything, so a job that dies
        # mid-flight and is retried returns the previous outcome for work already
        # done rather than performing a deletion twice (§25).
        await dsr_run_service.execute_queued_actions(
            uuid.UUID(job.payload["request_id"]),
            uuid.UUID(job.payload["org_id"]),
            job_id=job.id,
        )
    elif job.job_type == "incident_analysis":
        # Agent 4 (Breach Response) analysis: derive the affected-data map from ROPA,
        # seed the timeline from evidence, assess risk. Queued because it reads schema
        # baselines and iterates evidence, which should not sit inside a request.
        #
        # Containment is deliberately NOT a job type. A person performs a tracked
        # action and attests to it; a queued "disable account" job would be exactly the
        # fake execution the build forbids.
        await incident_run_service.run_analysis(
            uuid.UUID(job.payload["incident_id"]),
            uuid.UUID(job.payload["org_id"]),
            job_id=job.id,
        )
    elif job.job_type == "regwatch_collect":
        # Agent 5 (Regulatory Watch): fetch one approved source, compare it against
        # the accepted baseline, and raise a finding if anything moved -- including
        # when the fetch FAILED, which is a finding in its own right.
        await regwatch_run_service.run_collection(
            uuid.UUID(job.payload["source_id"]),
            uuid.UUID(job.payload["org_id"]),
            job_id=job.id,
        )
    elif job.job_type == "regwatch_assess":
        # Relevance, priority and impact are deterministic and always run; the
        # interpretation step is optional and its failure leaves an open question on
        # the finding rather than failing the job.
        await regwatch_run_service.run_assessment(
            uuid.UUID(job.payload["finding_id"]),
            uuid.UUID(job.payload["org_id"]),
            job_id=job.id,
        )
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
            await queue.mark_failed(db, job.id, str(exc))
            await db.commit()
    else:
        async with async_session_factory() as db:
            await queue.mark_done(db, job.id)
            await db.commit()


async def _maintenance_loop(stopping: asyncio.Event) -> None:
    """Stale-job reaping, the monitoring dispatcher and the two SLA sweeps.

    Every step is wrapped. On the old shared loop, reaping and dispatch were bare, so a
    failure there killed the process and `restart: unless-stopped` brought it back --
    crude, but it did not fail silently. On a dedicated task an unhandled exception
    would instead kill just this task and leave the pump running happily, which is
    exactly the silent stop the DSR sweep's own comment warns about. So each step
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

        # Agent 3 SLA sweep (§36). Rides this loop rather than adding a scheduler: a
        # DSR deadline is measured in days, so once every maintenance tick is far more
        # often than it needs to be. Best-effort -- a DB hiccup while flagging an SLA
        # must not stop the worker from processing jobs.
        try:
            async with async_session_factory() as db:
                breached = await dsr_sla_service.sweep_overdue(db)
                await db.commit()
            if breached:
                logger.warning("DSR: %d case(s) newly past their SLA", breached)
        except Exception:
            logger.exception("DSR SLA sweep failed; continuing with job processing")

        # Agent 4 incident sweep. Rides the same loop for the same reason, and flags
        # the organisation's own internal response target -- NOT a statutory deadline.
        # Whether a notification obligation applies is a decision for a person against
        # approved guidance, never a timer.
        try:
            async with async_session_factory() as db:
                overdue = await incident_sla_service.sweep_overdue(db)
                await db.commit()
            if overdue:
                logger.warning(
                    "Incidents: %d newly past their internal response target", overdue
                )
        except Exception:
            logger.exception("Incident sweep failed; continuing with job processing")

        # Agent 5 action sweep. Flags work whose own target date has passed -- the
        # organisation's date, never a statutory one, which every row it writes says
        # explicitly. Marks once per action and never again, because this loop runs
        # every tick and the audit log is append-only.
        try:
            async with async_session_factory() as db:
                overdue = await regwatch_action_sla_service.sweep_overdue(db)
                await db.commit()
            if overdue:
                logger.warning(
                    "Regulatory watch: %d action(s) newly past their target date", overdue
                )
        except Exception:
            logger.exception("Regulatory action sweep failed; continuing with job processing")

        # Agent 5 due-source sweep. Enqueues rather than collecting inline, so one
        # slow regulator cannot hold up every other organisation's watch. A source
        # whose interval has elapsed and which is NOT collected is the failure mode
        # this agent exists to make visible, so a failure here is logged loudly.
        try:
            enqueued = await regwatch_run_service.sweep_due_sources()
            if enqueued:
                logger.info("Regulatory watch: %d source collection(s) enqueued", enqueued)
        except Exception:
            logger.exception(
                "Regulatory watch sweep failed; due sources were NOT enqueued this tick"
            )

        await _sleep_or_stop(stopping, MAINTENANCE_INTERVAL_SECONDS)


async def _job_pump(stopping: asyncio.Event, concurrency: int) -> None:
    """Keep up to `concurrency` jobs running, and never await one inline."""
    in_flight: set[asyncio.Task] = set()

    while not stopping.is_set():
        claimed: list[AgentJob] = []
        free = concurrency - len(in_flight)
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
    concurrency = max(1, get_settings().worker_concurrency)
    logger.info("Worker %s starting with %d concurrent slot(s)", WORKER_ID, concurrency)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows' ProactorEventLoop; there the process is stopped
        # the blunt way and reap_stale_jobs cleans up, same as before.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    tasks = {
        asyncio.create_task(_maintenance_loop(stopping), name="maintenance"),
        asyncio.create_task(_job_pump(stopping, concurrency), name="job-pump"),
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
