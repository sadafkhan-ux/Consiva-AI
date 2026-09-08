import uuid

from app.agents.consent_agent.state import AgentState
from app.db.repositories import scan_repository


async def normalize(state: AgentState) -> dict:
    """Step 4 — loads the scan's already-persisted evidence into the working state.
    Nothing here is a judgment call; it's a straight read (docs/architecture §E). Uses
    the concurrent variant (7 tables fetched in parallel, not sequentially) since this
    is on the hot path of every analyze run — see get_scan_evidence_summary_concurrent's
    docstring."""
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(uuid.UUID(state.scan_id))
    return {"scan_evidence": evidence}
