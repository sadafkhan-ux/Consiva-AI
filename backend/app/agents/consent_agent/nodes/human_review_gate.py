import uuid

from langgraph.types import interrupt

from app.agents.consent_agent.state import AgentState
from app.db.repositories import finding_repository
from app.db.session import async_session_factory
from app.observability.stage_tracker import track_stage


async def human_review_gate(state: AgentState) -> dict:
    """Step 12. IMPORTANT: LangGraph re-runs a node from the top every time it resumes
    from an interrupt() inside it — so this function must stay a pure read followed by
    (maybe) a pause. It never writes anything to the graph's own business state. Each
    approve/reject/edit call in review_service.py writes directly to
    consent_findings/approvals and then attempts to resume this thread; if findings are
    still pending, this just re-pauses.

    The stage timer below is scoped to ONLY the read, not the interrupt() call itself
    (deliberately): interrupt() works by raising, and letting that propagate through
    track_stage's own exception handling would both record a pause as a "failed" stage
    and -- since this node re-runs from the top on every resume -- write a fresh
    failed-stage row on every single replay. Closing the tracked block first, then
    calling interrupt() outside it, keeps this node's replay-safety and the "never
    writes" invariant intact while still measuring the one real query it makes.

    Performance audit: this was the second of two completely untimed nodes in the
    graph (see normalize.py's docstring) -- the other candidate for the ~170s a real
    analyze run couldn't account for once every instrumented stage was subtracted out.
    """
    async with track_stage(
        uuid.UUID(state.scan_id), "human_review_gate", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        async with async_session_factory() as db:
            pending = await finding_repository.list_pending_review_for_agent_run(
                db, uuid.UUID(state.agent_run_id), uuid.UUID(state.org_id)
            )
        meta["pending_count"] = len(pending)

    if pending:
        interrupt({
            "reason": "findings_pending_human_review",
            "scan_id": state.scan_id,
            "pending_finding_ids": [str(f.id) for f in pending],
        })

    return {}
