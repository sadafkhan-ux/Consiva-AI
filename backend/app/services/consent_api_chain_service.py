"""Runs a whole API-requested scan: crawl, then analysis, then the webhook.

WHY THIS EXISTS

The console drives the Consent Agent in two steps -- POST a scan, wait, POST analyze --
because a person is sitting there and can decide whether to analyse what the crawl
found. An integrator has nobody watching: they POST a URL and poll one id until there
is a result. This job is the difference between those two shapes, and it is the only
new thing in the background layer.

It adds no scanning, analysis or reasoning of its own. It calls the same
`scan_service.execute_scan_and_persist` and `analysis_service` the console path uses,
in order, on the same worker, and then notifies.

WHY ONE JOB RATHER THAN CHAINING TWO

A crawl that fails must still notify, and a chain built by having the scan job enqueue
the analysis job has nowhere to put that: the scan job is already gone by the time
anyone knows the run is over. Keeping both phases inside one job means exactly one
place decides the run is finished and exactly one place sends the callback, whether the
run succeeded, degraded, or failed.
"""

from __future__ import annotations

import logging
import time
import uuid

from app.db.models import ConsentScan
from app.db.session import async_session_factory
from app.jobs import queue
from app.services import analysis_service, monitoring_service, scan_service, webhook_service

logger = logging.getLogger(__name__)


async def _scan_row(scan_id: uuid.UUID) -> ConsentScan | None:
    async with async_session_factory() as db:
        return await db.get(ConsentScan, scan_id)


async def _is_cancelled(scan_id: uuid.UUID) -> bool:
    scan = await _scan_row(scan_id)
    return scan is not None and scan.status == "cancelled"


async def run(scan_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Crawl, analyse, notify. Called by jobs/worker.py for `consent_api_chain`."""
    started = time.monotonic()
    scan = await _scan_row(scan_id)
    if scan is None:
        logger.warning("consent_api_chain: scan %s no longer exists; nothing to do", scan_id)
        return

    url = scan.url
    outcome = "failed"
    try:
        # Checked before each expensive phase rather than once at the top: cancellation
        # arrives while this job is running, which is precisely when it is worth
        # honouring. queue.cancel_jobs_for_scan cannot stop a job already in flight, so
        # this is what actually makes a mid-run cancel take effect.
        if await _is_cancelled(scan_id):
            logger.info("consent_api_chain: scan %s cancelled before crawl", scan_id)
            return

        await scan_service.execute_scan_and_persist(scan_id, url)
        await monitoring_service.maybe_run_diff_after_scan(scan_id)
        logger.info(
            "consent_agent.crawl.done scan_id=%s org_id=%s elapsed_ms=%d",
            scan_id, org_id, int((time.monotonic() - started) * 1000),
        )

        if await _is_cancelled(scan_id):
            logger.info("consent_api_chain: scan %s cancelled after crawl; skipping analysis", scan_id)
            return

        async with async_session_factory() as db:
            agent_run = await analysis_service.trigger_analysis(
                db, scan_id=scan_id, org_id=org_id, user_id=user_id,
                # This job runs the analysis itself, immediately below. Letting
                # trigger_analysis also queue an "analyze" job ran the whole analysis
                # twice -- measured live as rules_check/rag_retrieval/llm_analysis/
                # output_validation/findings_generated each appearing x2, and 6
                # findings on a 3-finding site.
                enqueue_job=False,
            )
            await db.commit()
            run_id = agent_run.id

        # Run the analysis inline rather than enqueuing it. Enqueuing would hand the
        # rest of this run to a different job, and then the webhook below would fire
        # before the findings exist.
        await analysis_service.run_analysis(run_id, scan_id, org_id)
        outcome = "completed"
        logger.info(
            "consent_agent.scan.completed scan_id=%s org_id=%s total_ms=%d",
            scan_id, org_id, int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        # Not swallowed: recorded, notified, then re-raised so the queue's own retry
        # and failure accounting still apply exactly as for every other job type.
        logger.exception("consent_agent.scan.failed scan_id=%s org_id=%s", scan_id, org_id)
        outcome = "failed"
        await _notify(scan_id, org_id, url, outcome, error=str(exc))
        raise
    else:
        await _notify(scan_id, org_id, url, outcome)


async def _notify(
    scan_id: uuid.UUID, org_id: uuid.UUID, url: str, status: str, error: str | None = None
) -> None:
    """Queue the webhook, if one was registered.

    Queued rather than sent here so delivery gets the job queue's retries and backoff,
    and so a slow or dead receiver cannot hold a worker slot open or turn a successful
    scan into a failed job.
    """
    scan = await _scan_row(scan_id)
    if scan is None or not scan.webhook_url:
        return
    event = "consent_scan.completed" if status == "completed" else "consent_scan.failed"
    payload = webhook_service.build_payload(
        scan_id=scan_id, status=status, website_url=url, event=event
    )
    if error:
        payload["error"] = error[:500]
    async with async_session_factory() as db:
        await queue.enqueue(
            db, org_id=org_id, job_type="consent_webhook",
            payload={
                "scan_id": str(scan_id), "org_id": str(org_id),
                "target_url": scan.webhook_url, "payload": payload,
            },
        )
        await db.commit()
    logger.info("consent_agent.webhook.queued scan_id=%s event=%s", scan_id, event)


async def deliver_webhook(payload: dict) -> None:
    """Called by jobs/worker.py for `consent_webhook`. Raises to trigger the queue's
    retry; the delivery record is written either way."""
    await webhook_service.deliver(
        org_id=uuid.UUID(payload["org_id"]),
        scan_id=uuid.UUID(payload["scan_id"]),
        target_url=payload["target_url"],
        payload=payload["payload"],
    )
