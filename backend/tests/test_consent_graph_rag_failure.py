"""A failed retrieval must not end the analysis with no findings.

Seen in production on consiva.ai ("Stage 'rag_retrieval' failed: Error code: 400 ..."): the embedding service
refused a query, the stage failed, and the whole run stopped -- although the rules had already matched.
Retrieval only supplies citations. The run now continues without the model and writes the rule-derived findings
(each flagged for human review, citing nothing), the same fallback a failed analysis already had.

The graph here is the REAL one from build_graph(), with each node replaced by a recorder, so what is asserted is
the order stages actually run in. No database, no model, no network.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agents.consent_agent import graph as graph_module
from app.agents.consent_agent.nodes import retrieve_rag
from app.agents.consent_agent.state import AgentState

NODES = (
    "normalize", "classify_rules", "retrieve_rag", "llm_reasoning", "validate_output",
    "create_findings", "create_rule_findings", "human_review_gate", "write_audit_log",
)


def _state(**kw):
    return AgentState(scan_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), agent_run_id=str(uuid.uuid4()), **kw)


def _wire(monkeypatch, *, rag_fails: bool):
    ran: list[str] = []

    def recorder(name, result=None):
        async def node(state):
            ran.append(name)
            return result or {}
        return node

    for name in NODES:
        result = None
        if name == "retrieve_rag":
            result = {"rag_failed": True, "error": "retrieval of regulatory provisions failed"} if rag_fails else {}
        if name == "validate_output":
            result = {"validation_status": "valid"}
        monkeypatch.setattr(graph_module, name, recorder(name, result))
    return ran


async def _run(monkeypatch, *, rag_fails: bool) -> list[str]:
    ran = _wire(monkeypatch, rag_fails=rag_fails)
    compiled = graph_module.build_graph().compile()
    await compiled.ainvoke(_state())
    return ran


async def test_when_retrieval_works_the_stages_run_in_the_documented_order(monkeypatch):
    ran = await _run(monkeypatch, rag_fails=False)

    assert ran == [
        "normalize", "classify_rules", "retrieve_rag", "llm_reasoning", "validate_output",
        "create_findings", "human_review_gate", "write_audit_log",
    ]


async def test_when_retrieval_fails_the_model_is_skipped_and_rule_findings_are_written(monkeypatch):
    ran = await _run(monkeypatch, rag_fails=True)

    assert ran == [
        "normalize", "classify_rules", "retrieve_rag", "create_rule_findings", "human_review_gate", "write_audit_log",
    ]
    assert "llm_reasoning" not in ran and "create_findings" not in ran
    assert ran.index("create_rule_findings") < ran.index("human_review_gate"), "still goes through human review"


@pytest.mark.parametrize("failed,expected", [(True, "create_rule_findings"), (False, "llm_reasoning")])
def test_the_route_after_retrieval(failed, expected):
    assert graph_module._route_after_retrieval(_state(rag_failed=failed)) == expected


def test_a_run_starts_with_retrieval_not_failed():
    assert _state().rag_failed is False


# ── the node ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def stage(monkeypatch):
    stages: list[dict] = []

    @asynccontextmanager
    async def tracked(scan_id, name, **kw):
        record = {"stage": name, "meta": {}, "error": None}
        stages.append(record)
        try:
            yield record["meta"]
        except Exception as exc:        # what the real track_stage does: record the failure, then re-raise
            record["error"] = str(exc)
            raise

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(retrieve_rag, "track_stage", tracked)
    monkeypatch.setattr(retrieve_rag, "async_session_factory", session)
    monkeypatch.setattr(retrieve_rag, "NvidiaLLMClient", lambda: object())
    return stages


def _node_state(*summaries):
    return SimpleNamespace(
        scan_id=str(uuid.uuid4()), agent_run_id=str(uuid.uuid4()),
        rule_findings=[{"summary": s} for s in summaries],
    )


async def test_a_retrieval_error_is_reported_on_the_stage_and_does_not_stop_the_run(monkeypatch, stage):
    async def refuses(queries, **kwargs):
        raise RuntimeError("Error code: 400 - {'error': 'This model does not support image input as query'}")

    monkeypatch.setattr(retrieve_rag, "retrieve_many", refuses)

    out = await retrieve_rag.retrieve_rag(_node_state("19 trackers fired before consent."))

    assert out["rag_failed"] is True and out["rag_chunks"] == []
    assert "retrieval of regulatory provisions failed" in out["error"] and "image input" in out["error"]
    # Not hidden: the stage row says it failed, and why.
    assert stage[0]["stage"] == "rag_retrieval" and "image input" in stage[0]["error"]


async def test_a_successful_retrieval_does_not_set_the_failure_flag(monkeypatch, stage):
    async def finds_nothing(queries, **kwargs):
        return [[] for _ in queries]

    monkeypatch.setattr(retrieve_rag, "retrieve_many", finds_nothing)

    out = await retrieve_rag.retrieve_rag(_node_state("19 trackers fired before consent."))

    assert out == {"rag_chunks": []} and stage[0]["error"] is None


async def test_the_error_text_is_bounded(monkeypatch, stage):
    async def refuses(queries, **kwargs):
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(retrieve_rag, "retrieve_many", refuses)

    out = await retrieve_rag.retrieve_rag(_node_state("a finding"))

    assert len(out["error"]) < 400


# ── the node records how it retrieved ────────────────────────────────────────────────────────

async def test_the_stage_says_when_the_chunks_were_found_by_keyword_because_the_embedding_service_did_not_answer(monkeypatch, stage):
    async def keyword_only(queries, *, stats, **kwargs):
        stats.update(mode="text_search", embedding_error="APITimeoutError", embed_calls=1, embed_ms=12000, search_ms=5, cache_hits=0)
        return [[] for _ in queries]

    monkeypatch.setattr(retrieve_rag, "retrieve_many", keyword_only)

    out = await retrieve_rag.retrieve_rag(_node_state("19 trackers fired before consent."))

    assert out == {"rag_chunks": []}                        # the run goes on to the model: nothing failed
    assert stage[0]["error"] is None
    assert stage[0]["meta"]["retrieval_mode"] == "text_search" and stage[0]["meta"]["embedding_error"] == "APITimeoutError"


async def test_the_stage_says_embedding_when_the_embedding_service_answered(monkeypatch, stage):
    async def normal(queries, *, stats, **kwargs):
        stats.update(mode="embedding", embed_calls=1, embed_ms=300, search_ms=5, cache_hits=0)
        return [[] for _ in queries]

    monkeypatch.setattr(retrieve_rag, "retrieve_many", normal)

    await retrieve_rag.retrieve_rag(_node_state("a finding"))

    assert stage[0]["meta"]["retrieval_mode"] == "embedding" and "embedding_error" not in stage[0]["meta"]
