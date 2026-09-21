"""Orchestration for Agent 5, and its entry points from the worker.

Lives beside ropa_run_service, dsr_run_service and incident_run_service for the same
reason they do: it is the seam between the platform (jobs, sessions, transactions) and
the agent's own logic, so app/jobs/worker.py imports one module per agent.

WHAT IS QUEUED, AND WHAT IS NOT
-------------------------------
Queued: collection (a network fetch of a regulator's page) and assessment (retrieval
plus, optionally, a model call). Both are slow and neither belongs inside a request.

NOT queued: anything a person decides, and anything a person does. There is no
`regwatch_act` job. An action raised by a finding is carried out by somebody and
attested to -- a worker that "performed" a compliance action would be the fake
execution the build forbids, in the one agent whose entire purpose is to be honest
about what is and is not known.

THE SWEEP
---------
`sweep_due_sources` rides the worker's maintenance loop and enqueues a collection for
every source whose interval has elapsed. It enqueues rather than collecting inline so
that one slow regulator cannot hold up the sweep for every other organisation.
"""

from __future__ import annotations

import logging
import uuid

from app.agents.regwatch.errors import RegWatchError
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import (
    assessment_service,
    collection_service,
    source_service,
)
from app.db.repositories import regwatch_repository as repo
from app.db.session import async_session_factory, set_org_scope
from app.jobs import queue

logger = logging.getLogger(__name__)


async def run_collection(
    source_id: uuid.UUID, org_id: uuid.UUID, job_id: uuid.UUID | None = None
) -> None:
    """Worker entry point for `regwatch_collect`.

    Collect the source, compare against the accepted baseline, and -- if that raised a
    finding -- queue its assessment. Collection and assessment are separate jobs on
    purpose: a fetch that succeeded should not be thrown away because the model behind
    the assessment was down.
    """
    set_org_scope(org_id)
    finding_id: uuid.UUID | None = None

    async with async_session_factory() as db:
        source = await repo.get_source(db, source_id, org_id)
        if source is None:
            # Not an error worth retrying: the source was deleted or belongs to
            # another org. Logged and dropped, because a job that raises here would
            # be retried three times to reach the same conclusion.
            logger.warning("regwatch_collect: source %s not found in org %s", source_id, org_id)
            return
        if not source.enabled:
            logger.info(
                "regwatch_collect: source %r is disabled; nothing collected", source.name
            )
            return

        collection = await collection_service.collect(db, source, job_id=job_id)
        change, finding = await collection_service.compare_and_record(db, source, collection)
        await db.commit()

        finding_id = finding.id if finding else None
        logger.info(
            "regwatch_collect: %s -> %s (%s)%s",
            source.name, collection.status, change.change_kind,
            f", finding {finding.reference}" if finding else "",
        )

    if finding_id is not None:
        async with async_session_factory() as db:
            await queue.enqueue(
                db, org_id=org_id, job_type="regwatch_assess",
                payload={"finding_id": str(finding_id), "org_id": str(org_id)},
            )
            await db.commit()


async def run_assessment(
    finding_id: uuid.UUID, org_id: uuid.UUID, job_id: uuid.UUID | None = None
) -> None:
    """Worker entry point for `regwatch_assess`.

    Everything deterministic runs regardless of whether the model is reachable; a
    model failure leaves an open question on the finding rather than a failed job.
    """
    set_org_scope(org_id)
    async with async_session_factory() as db:
        finding = await repo.get_finding(db, finding_id, org_id)
        if finding is None:
            logger.warning("regwatch_assess: finding %s not found in org %s", finding_id, org_id)
            return
        if finding.status != watch.DETECTED:
            # A supersede or a human decision landed between the enqueue and here.
            # Re-assessing would overwrite it.
            logger.info(
                "regwatch_assess: finding %s is %s, not %s; leaving it alone",
                finding.reference, finding.status, watch.DETECTED,
            )
            return

        try:
            await assessment_service.assess(db, finding)
        except RegWatchError:
            await db.rollback()
            raise
        await db.commit()
        logger.info(
            "regwatch_assess: %s -> %s (relevance %s / %s, priority %s)",
            finding.reference, finding.status, finding.relevance,
            finding.relevance_confidence, finding.priority,
        )


async def sweep_due_sources(limit: int = 100) -> int:
    """Enqueue a collection for every source whose check interval has elapsed.

    Returns how many were enqueued. Runs on the maintenance loop, across all orgs --
    the one deliberately unscoped read in Agent 5, for the same reason the DSR and
    incident sweeps are: a scheduler that could only see one tenant would silently
    stop watching every other one.
    """
    enqueued = 0
    async with async_session_factory() as db:
        due = await repo.list_due_sources(db, limit=limit)
        for source in due:
            await queue.enqueue(
                db, org_id=source.org_id, job_type="regwatch_collect",
                payload={"source_id": str(source.id), "org_id": str(source.org_id)},
            )
            enqueued += 1
        await db.commit()
    if enqueued:
        logger.info("Regulatory watch: enqueued %d due source collection(s)", enqueued)
    return enqueued


def unwatched_sources(sources: list) -> list[dict]:
    """Which sources are NOT currently being watched successfully.

    The spec's closing guardrail, as a function: monitoring failures must be visible,
    and the system must never report a source as current when collection failed. This
    is what the API and the dashboard both read, so there is one definition of
    "not current" rather than one per surface.
    """
    return [
        {"id": str(s.id), "name": s.name, "url": s.url, **source_service.health(s)}
        for s in sources
        if not source_service.health(s)["is_current"]
    ]
