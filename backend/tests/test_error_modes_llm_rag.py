"""Failure-mode tests for the LLM client and RAG retrieval path -- all fully mocked,
never touching the real NVIDIA API or a real DB (per project convention, see
test_llm_client.py's docstring). Traces each failure through to where it's ultimately
handled, using the *actual* current error-handling code (read before writing these
assertions):

  app/llm/client.py                                  -- tenacity retry + repair loop
  app/agents/consent_agent/nodes/llm_reasoning.py     -- no try/except of its own
  app/agents/consent_agent/nodes/retrieve_rag.py      -- no try/except of its own
  app/agents/consent_agent/nodes/validate_output.py   -- only handles ungrounded
                                                          citations, not malformed JSON
  app/agents/consent_agent/graph.py                   -- _route_after_validation only
                                                          runs *after* validate_output
                                                          returns; a node that raises
                                                          never reaches routing
  app/services/analysis_service.py                    -- the only place that catches a
                                                          node exception, marks the
                                                          agent_run "failed", and
                                                          re-raises
  app/observability/stage_tracker.py                  -- records ok=False + re-raises;
                                                          never swallows the wrapped
                                                          exception itself (only a
                                                          failure to *write* the record)
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.consent_agent.nodes.llm_reasoning import llm_reasoning
from app.agents.consent_agent.nodes.retrieve_rag import retrieve_rag
from app.agents.consent_agent.state import AgentState
from app.core.exceptions import LLMOutputValidationError
from app.db.models import AgentRun
from app.llm.client import NvidiaLLMClient
from app.rag.retriever import retrieve
from app.services.analysis_service import run_analysis


class _Schema(BaseModel):
    value: str


def _client_with_chat_error(exc: Exception) -> NvidiaLLMClient:
    """Same __new__ + fake-_client pattern as test_llm_client.py, but the fake
    create() always raises instead of returning a canned response."""
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client.provider = "nvidia"
    client.model_name = "test-model"
    client._extra_body = {}
    client._call_timeout = 60.0
    client._max_output_tokens = 4000
    fake_openai = MagicMock()
    fake_openai.chat.completions.create = AsyncMock(side_effect=exc)
    client._client = fake_openai
    return client


def _state(**overrides) -> AgentState:
    base = {
        "scan_id": "11111111-1111-1111-1111-111111111111",
        "org_id": "22222222-2222-2222-2222-222222222222",
        "agent_run_id": "33333333-3333-3333-3333-333333333333",
        "scan_evidence": {},
    }
    base.update(overrides)
    return AgentState(**base)


class _RecordingStage:
    """Drop-in replacement for observability.stage_tracker.track_stage that never
    opens a DB session, but reproduces its documented contract exactly: yields a
    mutable meta dict, and if the wrapped body raises, records the failure (instead
    of writing it to Postgres) and then re-raises -- it never swallows the body's own
    exception. Lets tests assert "was this stage recorded as failed?" without any DB
    dependency."""

    def __init__(self, calls: list[dict]):
        self._calls = calls

    def __call__(self, scan_id, stage, *, agent_run_id=None):
        return self._cm(stage)

    @asynccontextmanager
    async def _cm(self, stage):
        meta: dict = {}
        try:
            yield meta
        except Exception as exc:
            self._calls.append({"stage": stage, "ok": False, "error": str(exc)})
            raise
        else:
            self._calls.append({"stage": stage, "ok": True, "error": ""})


class _FakeSessionCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


# ---------------------------------------------------------------------------
# 1. NVIDIA API unavailable
# ---------------------------------------------------------------------------


async def test_generate_structured_propagates_after_nvidia_connection_failure():
    """create() always raises APIConnectionError -- confirm _chat's tenacity retry
    (stop_after_attempt(3), reraise=True) actually fires 3 times, and that
    generate_structured then propagates the real exception rather than hanging or
    returning empty/fake data. Note generate_structured's own repair loop only
    catches (ValidationError, json.JSONDecodeError) -- a connection error is never
    caught by it, so it surfaces immediately once _chat's own retries are exhausted."""
    req = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    client = _client_with_chat_error(openai.APIConnectionError(request=req))

    with pytest.raises(openai.APIConnectionError):
        await client.generate_structured(system="sys", user="usr", schema=_Schema)

    assert client._client.chat.completions.create.await_count == 3


# ---------------------------------------------------------------------------
# 2. NVIDIA timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build_error",
    [
        lambda req: openai.APITimeoutError(request=req),
        lambda req: TimeoutError("Request timed out"),
    ],
    ids=["openai_api_timeout_error", "builtin_timeout_error"],
)
async def test_generate_structured_propagates_after_nvidia_timeout(build_error):
    """Same graceful-failure contract as the connection-failure case above, for both
    a timeout raised by the openai SDK itself and a raw asyncio.TimeoutError -- tenacity's
    retry_if_exception_type(Exception) treats both identically."""
    req = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    error = build_error(req)
    client = _client_with_chat_error(error)

    with pytest.raises(type(error)):
        await client.generate_structured(system="sys", user="usr", schema=_Schema)

    assert client._client.chat.completions.create.await_count == 3


# ---------------------------------------------------------------------------
# 3. Malformed LLM response exhausting repair retries -> agent_run ends "failed"
# ---------------------------------------------------------------------------


async def test_llm_reasoning_propagates_llm_output_validation_error(monkeypatch):
    """generate_structured's own basic retry-then-raise behavior is already covered
    by test_llm_client.py::test_generate_structured_raises_after_exhausting_retries.
    This traces what happens *above* that: llm_reasoning.py has no try/except of its
    own around generate_structured, so LLMOutputValidationError propagates straight
    through it. track_stage still records the stage as failed (ok=False, error
    captured) before re-raising -- the exception is never swallowed at this layer."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.agents.consent_agent.nodes.llm_reasoning.track_stage", _RecordingStage(calls)
    )
    monkeypatch.setattr(
        "app.agents.consent_agent.nodes.llm_reasoning.generate_structured_with_fallback",
        AsyncMock(side_effect=LLMOutputValidationError(
            "LLM failed to produce valid ConsentAnalysisResponse after 3 attempts: bad json"
        )),
    )

    with pytest.raises(LLMOutputValidationError, match="bad json"):
        await llm_reasoning(_state())

    assert calls == [{"stage": "llm_analysis", "ok": False, "error": calls[0]["error"]}]
    assert "bad json" in calls[0]["error"]


async def test_run_analysis_marks_agent_run_failed_on_llm_output_validation_error(monkeypatch):
    """Full trace to app/services/analysis_service.py: graph.ainvoke() raising
    LLMOutputValidationError (as it would once llm_reasoning propagates one, since
    graph.py's _route_after_validation is only ever consulted *after* validate_output
    returns normally -- a node exception bypasses that routing table entirely) is
    caught by run_analysis's try/except, which sets agent_run.status="failed" and
    agent_run.error to the message -- not an unhandled crash. run_analysis then
    re-raises the original exception by design (see app/jobs/worker.py's
    run_forever(), which wraps _process_one in its own broad except Exception and
    marks the *job* row failed -- so the worker process itself does not crash either;
    that outer layer is outside this file's scope but is what makes the re-raise
    here safe)."""
    agent_run_id, scan_id, org_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agent_run = AgentRun(id=agent_run_id, scan_id=scan_id, agent_name="consent_agent", status="pending")
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=agent_run)
    fake_session.commit = AsyncMock()
    monkeypatch.setattr(
        "app.services.analysis_service.async_session_factory", lambda: _FakeSessionCM(fake_session)
    )

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(
        side_effect=LLMOutputValidationError(
            "LLM failed to produce valid ConsentAnalysisResponse after 3 attempts: bad json"
        )
    )
    monkeypatch.setattr(
        "app.services.analysis_service.get_compiled_graph", AsyncMock(return_value=fake_graph)
    )

    with pytest.raises(LLMOutputValidationError):
        await run_analysis(agent_run_id, scan_id, org_id)

    assert agent_run.status == "failed"
    assert agent_run.error is not None and "bad json" in agent_run.error
    assert agent_run.completed_at is not None


# ---------------------------------------------------------------------------
# 4. RAG retrieval failure
# ---------------------------------------------------------------------------


async def test_retrieve_propagates_db_execute_failure():
    """app.rag.retriever.retrieve has no try/except around db.execute() -- confirm a
    DB failure during the pgvector query propagates as-is rather than being caught
    and turned into a false-empty [] result (which would silently tell the rest of
    the pipeline "no relevant knowledge found" instead of "retrieval failed")."""
    fake_llm_client = MagicMock()
    fake_llm_client.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    fake_db = MagicMock(spec=AsyncSession)
    db_error = OperationalError("SELECT ...", {}, Exception("connection refused"))
    fake_db.execute = AsyncMock(side_effect=db_error)

    with pytest.raises(OperationalError):
        await retrieve("some query", db=fake_db, llm_client=fake_llm_client, top_k=4)

    fake_llm_client.embed.assert_awaited_once()
    fake_db.execute.assert_awaited_once()


async def test_retrieve_rag_node_records_failed_stage_and_propagates(monkeypatch):
    """Traces the same failure one level up into the retrieve_rag node: does
    track_stage's failure-recording or anything upstream swallow it silently?
    Answer, confirmed by this test: no -- the stage is recorded as failed (ok=False,
    error captured) and the original DB exception still propagates out of the node
    unchanged, exactly like the llm_reasoning case above."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.agents.consent_agent.nodes.retrieve_rag.track_stage", _RecordingStage(calls)
    )
    fake_llm_client = MagicMock()
    fake_llm_client.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    monkeypatch.setattr(
        "app.agents.consent_agent.nodes.retrieve_rag.NvidiaLLMClient", MagicMock(return_value=fake_llm_client)
    )
    fake_db = MagicMock(spec=AsyncSession)
    db_error = OperationalError("SELECT ...", {}, Exception("connection refused"))
    fake_db.execute = AsyncMock(side_effect=db_error)
    monkeypatch.setattr(
        "app.agents.consent_agent.nodes.retrieve_rag.async_session_factory", lambda: _FakeSessionCM(fake_db)
    )

    with pytest.raises(OperationalError):
        await retrieve_rag(_state(rule_findings=[]))

    assert len(calls) == 1
    assert calls[0]["stage"] == "rag_retrieval"
    assert calls[0]["ok"] is False
    assert "connection refused" in calls[0]["error"]
