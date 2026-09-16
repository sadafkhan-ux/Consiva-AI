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


# ── One invented citation must not discard the findings that were grounded ──────
# Live run: 6 findings drafted, 1 ungrounded id (an EVIDENCE id the model had put in
# dpdp_reference), and the whole analysis was re-generated -- a second full LLM cycle
# on top of a 365.7s first attempt. The grounded findings were thrown away with it.

async def test_an_ungrounded_citation_no_longer_discards_the_grounded_findings():
    state = _state(
        llm_output={"findings": [
            _finding(finding="grounded", dpdp_reference=["c1"]),
            _finding(finding="bad", dpdp_reference=["evidence-id-not-a-chunk"]),
        ]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
        validation_attempts=0,
    )

    result = await validate_output(state)

    assert result["validation_status"] == "valid", "one bad reference re-ran the whole analysis"
    findings = result["llm_output"]["findings"]
    assert findings[0]["dpdp_reference"] == ["c1"], "a correctly grounded finding was altered"


async def test_the_invented_citation_is_removed_and_its_finding_forced_to_review():
    """Stripping is only safe because the claim cannot then reach a customer unreviewed:
    the id is gone, and the uncited-findings backstop forces a human onto it."""
    state = _state(
        llm_output={"findings": [
            _finding(dpdp_reference=["c1"]),
            _finding(dpdp_reference=["invented"], requires_human_review=False),
        ]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
    )

    result = await validate_output(state)

    stripped = result["llm_output"]["findings"][1]
    assert "invented" not in stripped["dpdp_reference"], "an invented citation survived"
    assert stripped["requires_human_review"] is True, "a stripped finding could be auto-approved"


async def test_every_finding_ungrounded_still_retries():
    """The narrowing applies to a slip on one reference. If NOTHING is grounded the
    model has misunderstood the task, and re-asking is still the right answer."""
    state = _state(
        llm_output={"findings": [
            _finding(dpdp_reference=["nope-1"]),
            _finding(dpdp_reference=["nope-2"]),
        ]},
        rag_chunks=[{"chunk_id": "c1", "document_title": "DPDP Rules"}],
        validation_attempts=0,
    )

    result = await validate_output(state)

    assert result["validation_status"] == "retry"
