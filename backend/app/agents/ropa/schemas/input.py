"""What the ROPA agent graph is invoked with (prompt §2). Distinct from
state.py's AgentState -- this is the caller-facing contract (ropa_service.py
building a run), not the internal graph state threaded between nodes."""

from pydantic import BaseModel, Field

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import RopaRecord


class RopaAgentInput(BaseModel):
    org_id: str
    agent_run_id: str
    evidence: DiscoveryEvidence

    # Previously approved ROPA, supplied so the agent can diff against it
    # instead of rebuilding blind (prompt §20) and so it never silently
    # rewrites an approved record (prompt §6, §19).
    baseline_ropa_records: list[RopaRecord] = Field(default_factory=list)

    # Minimum confidence below which a finding must be routed to human review
    # (prompt §19). Configured per org, not hardcoded in the prompt.
    review_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
