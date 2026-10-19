from typing import Literal

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    scan_id: str
    org_id: str
    agent_run_id: str

    scan_evidence: dict | None = None          # get_scan_evidence_summary() output, set by normalize
    rule_findings: list[dict] = Field(default_factory=list)  # RuleFinding dicts, set by classify_rules
    rag_chunks: list[dict] = Field(default_factory=list)
    # True when retrieval of regulatory provisions failed (the embedding service refused or was unreachable).
    # The run then skips the model and writes the rule-derived findings, exactly as it does when the model
    # fails: losing the citations should cost the explanation, not the finding.
    rag_failed: bool = False
    llm_output: dict | None = None             # serialized ConsentAnalysisResponse
    validation_status: Literal["pending", "valid", "retry", "failed"] = "pending"
    validation_attempts: int = 0
    # Wall-clock (epoch seconds) budget for this analysis's ENTIRE LLM effort, set once
    # by llm_reasoning on its first invocation and honored by every retry layer -- the
    # graph-level validation retry (validate_output), generate_structured's schema
    # retry, and (indirectly, by refusing to start a new attempt past it) the network
    # retry. Without it the three layers compose to a 27-real-HTTP-calls worst case.
    llm_deadline_epoch: float | None = None
    created_finding_ids: list[str] = Field(default_factory=list)
    error: str | None = None
