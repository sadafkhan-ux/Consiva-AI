import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.consent_agent.graph import get_compiled_graph
from app.agents.consent_agent.state import AgentState
from app.config import get_settings
from app.core.exceptions import AgentRunAlreadyInProgressError, NotFoundError, ScanNotReadyError
from app.db.models import AgentRun
from app.db.repositories import scan_repository
from app.db.session import async_session_factory
from app.jobs import queue
from app.llm.client import resolve_reasoning_provider_and_model
from app.services import audit_service

logger = logging.getLogger(__name__)

_NON_TERMINAL_AGENT_RUN_STATUSES = ("pending", "running", "paused")


async def trigger_analysis(db: AsyncSession, *, scan_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID) -> AgentRun:
    scan = await scan_repository.get_scan(db, scan_id, org_id)
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    if scan.status != "completed":
        raise ScanNotReadyError(f"Scan {scan_id} is not completed yet (status={scan.status})")

    existing = await db.execute(
        select(AgentRun).where(
            AgentRun.scan_id == scan_id, AgentRun.status.in_(_NON_TERMINAL_AGENT_RUN_STATUSES)
        )
    )
    in_progress = existing.scalar_one_or_none()
    if in_progress is not None:
        # A second /analyze call while the first hasn't reached a terminal status
        # previously created a fully independent AgentRun + analyze job -- silently
        # doubling real NVIDIA LLM cost and producing two overlapping finding sets for
        # the same scan. The right response IS the already-in-flight run, not a new one.
        raise AgentRunAlreadyInProgressError(
            f"Analysis is already in progress for scan {scan_id} "
            f"(agent_run={in_progress.id}, status={in_progress.status})"
        )

    agent_run = AgentRun(scan_id=scan_id, agent_name="consent_agent", status="pending")
    db.add(agent_run)
    await db.flush()

    await audit_service.record(
        db, org_id=scan.org_id, actor_user_id=user_id, action="agent_run.requested",
        entity_type="agent_run", entity_id=agent_run.id, agent_run_id=agent_run.id,
    )
    await queue.enqueue(
        db, org_id=scan.org_id, job_type="analyze",
        payload={"agent_run_id": str(agent_run.id), "scan_id": str(scan_id), "org_id": str(scan.org_id)},
    )
    await db.commit()
    return agent_run


async def run_analysis(agent_run_id: uuid.UUID, scan_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Called by jobs/worker.py for job_type="analyze". Owns its own session for the
    setup/teardown around the graph invocation; the graph's own nodes open their own
    sessions per step (see agents/consent_agent/nodes/*)."""
    settings = get_settings()
    thread_id = str(agent_run_id)

    async with async_session_factory() as db:
        agent_run = await db.get(AgentRun, agent_run_id)
        if agent_run is None:
            return
        agent_run.status = "running"
        agent_run.started_at = datetime.now(UTC)
        # Recorded up front from the ACTUAL configured provider (settings.llm_provider),
        # never hardcoded to NVIDIA -- this is what makes agent_runs.llm_provider (a
        # column that already existed but was never actually set) and llm_model
        # trustworthy audit/traceability data instead of a fixed label.
        agent_run.llm_provider, agent_run.llm_model = resolve_reasoning_provider_and_model(settings)
        agent_run.langgraph_thread_id = thread_id
        await db.commit()

    graph = await get_compiled_graph()
    initial_state = AgentState(scan_id=str(scan_id), org_id=str(org_id), agent_run_id=str(agent_run_id))

    try:
        result = await graph.ainvoke(initial_state.model_dump(), config={"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        logger.exception("Agent run %s crashed", agent_run_id)
        async with async_session_factory() as db:
            agent_run = await db.get(AgentRun, agent_run_id)
            if agent_run:
                agent_run.status = "failed"
                agent_run.error = str(exc)
                agent_run.completed_at = datetime.now(UTC)
                await db.commit()
        raise

    if "__interrupt__" in result:
        async with async_session_factory() as db:
            agent_run = await db.get(AgentRun, agent_run_id)
            if agent_run and agent_run.status != "completed":
                agent_run.status = "paused"
                await db.commit()
    # Otherwise the graph ran to completion — its write_audit_log node already set
    # agent_run.status to "completed" or "failed" in its own session.
