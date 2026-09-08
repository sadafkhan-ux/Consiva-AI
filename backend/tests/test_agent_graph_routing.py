"""Pure/mocked tests for the agent graph's decision logic — no Postgres checkpointer,
no LLM call. get_compiled_graph() itself needs a real Postgres connection (the
LangGraph checkpointer) and isn't exercised here; see docs troubleshooting notes."""

from app.agents.consent_agent.graph import _route_after_validation
from app.agents.consent_agent.nodes.validate_output import validate_output
from app.agents.consent_agent.state import AgentState


def _finding(**overrides) -> dict:
    base = {
        "finding": "x", "category": "analytics", "risk_level": "medium", "priority": "medium",
        "evidence": [], "dpdp_reference": [], "recommendation": "x", "requires_human_review": True,
    }
    base.update(overrides)
    return base


def _state(**overrides) -> AgentState:
    base = {
        "scan_id": "11111111-1111-1111-1111-111111111111",
        "org_id": "22222222-2222-2222-2222-222222222222",
        "agent_run_id": "33333333-3333-3333-3333-333333333333",
    }
    base.update(overrides)
    return AgentState(**base)


async def test_validate_output_marks_valid_when_all_citations_grounded():
    state = _state(
        llm_output={"findings": [_finding(dpdp_reference=["c1"])]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
    )
    result = await validate_output(state)
    assert result["validation_status"] == "valid"
    assert result["error"] is None


async def test_validate_output_retries_on_ungrounded_citation():
    state = _state(
        llm_output={"findings": [_finding(dpdp_reference=["not-retrieved"])]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
        validation_attempts=0,
    )
    result = await validate_output(state)
    assert result["validation_status"] == "retry"
    assert result["validation_attempts"] == 1
    assert "not-retrieved" in result["error"]


async def test_validate_output_fails_after_max_attempts():
    state = _state(
        llm_output={"findings": [_finding(dpdp_reference=["not-retrieved"])]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
        validation_attempts=2,  # MAX_VALIDATION_ATTEMPTS
    )
    result = await validate_output(state)
    assert result["validation_status"] == "failed"
    assert "not-retrieved" in result["error"]


async def test_validate_output_passes_with_no_citations_at_all():
    state = _state(llm_output={"findings": [_finding(dpdp_reference=[])]}, rag_chunks=[])
    result = await validate_output(state)
    assert result["validation_status"] == "valid"


def test_route_after_validation_maps_every_status():
    assert _route_after_validation(_state(validation_status="valid")) == "create_findings"
    assert _route_after_validation(_state(validation_status="retry")) == "llm_reasoning"
    assert _route_after_validation(_state(validation_status="failed")) == "write_audit_log"
