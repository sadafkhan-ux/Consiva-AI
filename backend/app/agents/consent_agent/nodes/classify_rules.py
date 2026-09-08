import uuid

from app.agents.consent_agent.state import AgentState
from app.observability.stage_tracker import track_stage
from app.rules.consent_rules import evaluate_consent_rules


async def classify_rules(state: AgentState) -> dict:
    """Steps 5-6 — deterministic classification/rule checks over the normalized
    evidence. Pure and synchronous; no I/O, no LLM call."""
    async with track_stage(uuid.UUID(state.scan_id), "rules_check", agent_run_id=uuid.UUID(state.agent_run_id)) as meta:
        findings = evaluate_consent_rules(state.scan_evidence)
        meta["rule_findings_count"] = len(findings)
        meta["rule_ids"] = [f.rule_id for f in findings]
    return {"rule_findings": [f.model_dump() for f in findings]}
