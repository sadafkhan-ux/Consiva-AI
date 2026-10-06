import uuid

from app.agents.consent_agent.state import AgentState
from app.db.repositories import scan_repository
from app.observability.stage_tracker import track_stage


async def normalize(state: AgentState) -> dict:
    """Step 4 — loads the scan's already-persisted evidence into the working state.
    Nothing here is a judgment call; it's a straight read (docs/architecture §E). Uses
    the concurrent variant (7 tables fetched in parallel, not sequentially) since this
    is on the hot path of every analyze run — see get_scan_evidence_summary_concurrent's
    docstring.

    Tracked (performance audit): this was the first of two nodes in the graph with no
    stage timer at all, and a real analyze run showed ~170s unaccounted for inside the
    job's own execution once every OTHER node's own stage timings were subtracted out.
    This can't explain that gap by itself on a quick read, but it's one of exactly two
    places the gap could be hiding, so it gets measured rather than assumed clean.
    """
    async with track_stage(
        uuid.UUID(state.scan_id), "normalize_evidence", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        evidence = await scan_repository.get_scan_evidence_summary_concurrent(uuid.UUID(state.scan_id))
        for key, value in evidence.items():
            if isinstance(value, list):
                meta[f"{key}_count"] = len(value)
    return {"scan_evidence": evidence}
