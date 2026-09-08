import uuid

from langgraph.types import interrupt

from app.agents.consent_agent.state import AgentState
from app.db.repositories import finding_repository
from app.db.session import async_session_factory


async def human_review_gate(state: AgentState) -> dict:
    """Step 12. IMPORTANT: LangGraph re-runs a node from the top every time it resumes
    from an interrupt() inside it — so this function must stay a pure read followed by
    (maybe) a pause. It never writes anything. Each approve/reject/edit call in
    review_service.py writes directly to consent_findings/approvals and then attempts
    to resume this thread; if findings are still pending, this just re-pauses.
    """
    async with async_session_factory() as db:
        pending = await finding_repository.list_pending_review_for_agent_run(
            db, uuid.UUID(state.agent_run_id), uuid.UUID(state.org_id)
        )

    if pending:
        interrupt({
            "reason": "findings_pending_human_review",
            "scan_id": state.scan_id,
            "pending_finding_ids": [str(f.id) for f in pending],
        })

    return {}
