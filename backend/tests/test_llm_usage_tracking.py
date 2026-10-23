"""LLM usage tracking (app/llm/usage_tracking.py, migration 0041, the shared client): what is recorded for every call, how it is priced, and that a
failure to record never disturbs the call. No network and no database: providers are stand-ins and the table's own rules are read from its SQL."""

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.core.exceptions import LLMOutputValidationError
from app.db.models import LlmUsageEvent
from app.db.repositories import usage_repository
from app.llm import client as llm_client
from app.llm import usage_tracking as ut
from app.llm.schemas import ConsentAnalysisResponse

SCAN_A, SCAN_B, RUN = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

CLAUDE_USAGE = {
    "input_tokens": 11841, "output_tokens": 3590, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}, "output_tokens_details": {"thinking_tokens": 2304}, "request_id": "req_abc",
}


# ── what a call's usage adds up to ────────────────────────────────────────────────────────────────────────────────


def test_claude_usage_is_read_as_the_provider_reported_it_and_reasoning_is_not_added_again():
    s = ut.summarise_usage([CLAUDE_USAGE])
    assert (s["input_tokens"], s["output_tokens"]) == (11841, 3590)
    assert s["reasoning_tokens"] == 2304 and s["output_tokens"] == 3590        # the reasoning is inside the output total, never on top of it
    assert s["request_ids"] == ["req_abc"]


def test_several_attempts_add_up_and_each_request_keeps_its_id():
    s = ut.summarise_usage([CLAUDE_USAGE, {**CLAUDE_USAGE, "input_tokens": 100, "output_tokens": 50, "request_id": "req_def", "cache_read_input_tokens": 40,
                                          "cache_creation_input_tokens": 7, "cache_creation": {"ephemeral_1h_input_tokens": 7}}])
    assert (s["input_tokens"], s["output_tokens"], s["cache_read_tokens"], s["cache_write_tokens"], s["cache_write_1h_tokens"]) == (11941, 3640, 40, 7, 7)
    assert s["request_ids"] == ["req_abc", "req_def"] and s["reasoning_tokens"] == 4608


def test_the_openai_compatible_shape_is_understood():
    s = ut.summarise_usage([{"prompt_tokens": 900, "completion_tokens": 120, "prompt_tokens_details": {"cached_tokens": 64},
                             "completion_tokens_details": {"reasoning_tokens": 30}, "request_id": "chatcmpl-1"}])
    assert (s["input_tokens"], s["output_tokens"], s["cache_read_tokens"], s["reasoning_tokens"], s["request_ids"]) == (900, 120, 64, 30, ["chatcmpl-1"])


@pytest.mark.parametrize("blank", [[], [None], [{}], [{"input_tokens": None, "output_tokens": None}]])
def test_missing_usage_is_zero_with_reasoning_unknown_not_invented(blank):
    s = ut.summarise_usage(blank)
    assert s["input_tokens"] == 0 and s["output_tokens"] == 0 and s["reasoning_tokens"] is None and s["request_ids"] == []


# ── price ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_call_is_priced_at_the_published_haiku_rate():
    # The real policy call on 2026-10-23 logged 7,074 in / 4,456 out at $0.0029354: $0.10 and $0.50 per million tokens.
    assert ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=7074, output_tokens=4456) == pytest.approx(0.0029354, abs=1e-9)
    assert ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=11841, output_tokens=3590) == pytest.approx(0.0029791, abs=1e-9)


def test_cache_reads_and_writes_have_their_own_rates_and_input_excludes_them():
    c = ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=1_000, output_tokens=0, cache_read_tokens=10_000, cache_write_tokens=2_000, cache_write_1h_tokens=500)
    assert c == pytest.approx((1_000 * 0.10 + 10_000 * 0.01 + 1_500 * 0.125 + 500 * 0.20) / 1e6, abs=1e-12)


def test_a_prompt_over_100k_tokens_pays_haiku_long_prompt_prices_counting_cache_tokens():
    assert ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000) == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=100_001, output_tokens=1_000) == pytest.approx((100_001 * 0.50 + 1_000 * 2.50) / 1e6)
    assert ut.compute_cost("claude", "claude-haiku-5-5", input_tokens=60_000, output_tokens=0, cache_read_tokens=50_000) == pytest.approx((60_000 * 0.50 + 50_000 * 0.05) / 1e6)


@pytest.mark.parametrize("provider,model", [("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"), ("self_hosted", "qwen"), ("claude", "claude-unknown-9")])
def test_an_unpriced_model_has_no_cost_never_zero(provider, model):
    assert ut.compute_cost(provider, model, input_tokens=1000, output_tokens=1000) is None


# ── the event row ─────────────────────────────────────────────────────────────────────────────────────────────────


def _event(**over):
    base = {
        "provider": "claude", "model": "claude-haiku-5-5", "agent": "consent", "usage_sink": {"usage_by_attempt": [CLAUDE_USAGE]}, "fallback_meta": {}, "scan_id": SCAN_A,
        "agent_run_id": RUN, "stage": "llm_analysis", "status": "success", "duration_ms": 9500, "call_group": uuid.uuid4(), "role": "primary",
    }
    return ut.build_event(**{**base, **over})


def test_an_event_carries_who_what_how_much_and_how_long():
    e = _event()
    assert e["scan_id"] == uuid.UUID(SCAN_A) and e["agent_run_id"] == uuid.UUID(RUN) and e["stage"] == "llm_analysis" and e["agent"] == "consent"
    assert (e["provider"], e["model"], e["status"], e["duration_ms"], e["attempts"]) == ("claude", "claude-haiku-5-5", "success", 9500, 1)
    assert (e["input_tokens"], e["output_tokens"], e["reasoning_tokens"], e["request_ids"]) == (11841, 3590, 2304, ["req_abc"])
    assert e["cost_usd"] == pytest.approx(0.002979, abs=1e-6) and e["currency"] == "USD" and "platform.claude.com" in e["price_source"]


def test_an_unpriced_event_says_so():
    e = _event(provider="nvidia", model="nemotron")
    assert e["cost_usd"] is None and e["price_source"] is None


def test_the_same_call_reported_twice_has_the_same_id_and_a_different_provider_or_role_does_not():
    g = uuid.uuid4()
    assert _event(call_group=g)["call_id"] == _event(call_group=g)["call_id"]
    assert _event(call_group=g)["call_id"] != _event(call_group=g, provider="nvidia", model="m", role="fallback")["call_id"]
    assert _event(call_group=g, role="primary")["call_id"] != _event(call_group=g, role="fallback")["call_id"]
    assert _event()["call_id"] != _event()["call_id"]                      # two separate calls are two rows


def test_only_a_call_tied_to_a_scan_gets_a_row():
    assert _event(scan_id=None) is None and _event(scan_id="not-a-uuid") is None
    assert _event(agent_run_id="junk")["agent_run_id"] is None             # a bad run id loses the link, not the row


def test_an_event_holds_no_prompt_or_secret():
    e = _event(usage_sink={"usage_by_attempt": [{**CLAUDE_USAGE, "api_key": "sk-secret", "messages": ["private page text"]}]})
    blob = json.dumps({k: str(v) for k, v in e.items()})
    assert "sk-secret" not in blob and "private page text" not in blob


def test_the_insert_does_nothing_when_the_call_is_already_stored():
    sql = str(usage_repository.pg_insert(LlmUsageEvent).values(call_id=uuid.uuid4()).on_conflict_do_nothing(index_elements=["call_id"]).compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (call_id) DO NOTHING" in sql


def test_the_table_and_the_model_agree():
    body = (Path(__file__).resolve().parents[1] / "migrations" / "0041_llm_usage_events.sql").read_text(encoding="utf-8")
    columns = set(re.findall(r"^\s{4}(\w+)\s+(?:uuid|text|integer|boolean|numeric|timestamptz)\b", body, re.MULTILINE))
    later = (Path(__file__).resolve().parents[1] / "migrations" / "0042_llm_usage_page_urls.sql").read_text(encoding="utf-8")
    columns |= set(re.findall(r"add column if not exists (\w+)", later))        # 0042 adds the page list
    assert columns == {c.name for c in LlmUsageEvent.__table__.columns}
    assert "call_id            uuid not null unique" in body and "references consent_scans(id) on delete cascade" in body
    assert "enable row level security" in body and "tenant_isolation" in body


# ── the shared client records every call, failures included ───────────────────────────────────────────────────────


class _Provider:
    """A stand-in provider. `usage` is what it bills; `fail` makes it raise after billing."""

    def __init__(self, provider, model, usage, fail=None):
        self.provider, self.model_name, self._usage, self._fail = provider, model, usage, fail

    async def generate_structured(self, *, system, user, schema, max_attempts, usage_sink, deadline_epoch):
        usage_sink["attempts"] = len(self._usage)
        usage_sink["usage_by_attempt"] = list(self._usage)
        usage_sink["chat_retries"] = []
        if self._fail:
            raise self._fail
        return schema(findings=[])


@pytest.fixture
def wired(monkeypatch):
    """The client with stand-in providers, and every recorded event captured instead of stored."""
    events: list[dict] = []

    async def capture(**kw):
        events.append(kw)

    monkeypatch.setattr(ut, "record_llm_event", capture)
    settings = get_settings().model_copy(update={"anthropic_api_key": "k", "llm_provider": "nvidia"})
    return events, settings, monkeypatch


def _call(settings, **kw):
    sink: dict = {}
    result = asyncio.run(llm_client.generate_structured_with_fallback(system="s", user="u", schema=ConsentAnalysisResponse, settings=settings, usage_sink=sink, **kw))
    return result, sink


def test_a_successful_call_is_recorded_once_with_its_stage_run_and_scan(wired):
    events, settings, mp = wired
    mp.setattr(llm_client, "ClaudeLLMClient", lambda s: _Provider("claude", "claude-haiku-5-5", [CLAUDE_USAGE]))
    (_, meta), sink = _call(settings, agent="consent", scan_id=SCAN_A, stage="llm_analysis", agent_run_id=RUN)
    assert len(events) == 1 and events[0]["status"] == "success" and events[0]["role"] == "primary"
    assert (events[0]["scan_id"], events[0]["stage"], events[0]["agent_run_id"], events[0]["provider"]) == (SCAN_A, "llm_analysis", RUN, "claude")
    assert sink["usage_by_attempt"] == [CLAUDE_USAGE] and meta["primary_status"] == "success"


def test_a_failed_primary_that_was_billed_is_recorded_and_not_overwritten_by_the_fallback(wired):
    events, settings, mp = wired
    mp.setattr(llm_client, "ClaudeLLMClient", lambda s: _Provider("claude", "claude-haiku-5-5", [CLAUDE_USAGE, CLAUDE_USAGE], fail=LLMOutputValidationError("declined")))
    mp.setattr(llm_client, "get_reasoning_llm_client", lambda s: _Provider("nvidia", "nemotron", [{"prompt_tokens": 500, "completion_tokens": 80}]))
    (_, meta), sink = _call(settings, agent="consent", scan_id=SCAN_A, stage="llm_analysis")
    assert [(e["provider"], e["status"], e["role"]) for e in events] == [("claude", "failed", "primary"), ("nvidia", "success", "fallback")]
    assert events[0]["error_type"] == "LLMOutputValidationError" and len(events[0]["usage_sink"]["usage_by_attempt"]) == 2   # the primary's two billed attempts survive
    assert len(events[1]["usage_sink"]["usage_by_attempt"]) == 1
    assert sink["usage_by_attempt"] == [{"prompt_tokens": 500, "completion_tokens": 80}] and meta["fallback_triggered"] is True   # the caller still sees the winner's usage


def test_when_every_provider_fails_both_are_recorded_and_the_error_still_reaches_the_caller(wired):
    events, settings, mp = wired
    mp.setattr(llm_client, "ClaudeLLMClient", lambda s: _Provider("claude", "claude-haiku-5-5", [CLAUDE_USAGE], fail=LLMOutputValidationError("a")))
    mp.setattr(llm_client, "get_reasoning_llm_client", lambda s: _Provider("nvidia", "nemotron", [], fail=LLMOutputValidationError("b")))
    with pytest.raises(LLMOutputValidationError, match="b"):
        _call(settings, agent="consent", scan_id=SCAN_A)
    assert [(e["provider"], e["status"]) for e in events] == [("claude", "failed"), ("nvidia", "failed")]


def test_two_scans_running_at_once_never_mix_their_usage(wired):
    events, settings, mp = wired
    mp.setattr(llm_client, "ClaudeLLMClient", lambda s: _Provider("claude", "claude-haiku-5-5", [{**CLAUDE_USAGE, "input_tokens": 111}]))

    async def both():
        return await asyncio.gather(
            llm_client.generate_structured_with_fallback(system="s", user="u", schema=ConsentAnalysisResponse, settings=settings, usage_sink={}, agent="consent", scan_id=SCAN_A, stage="llm_analysis"),
            llm_client.generate_structured_with_fallback(system="s", user="u", schema=ConsentAnalysisResponse, settings=settings, usage_sink={}, agent="consent-policy", scan_id=SCAN_B, stage="policy_analysis"),
        )

    asyncio.run(both())
    by_scan = {e["scan_id"]: e for e in events}
    assert set(by_scan) == {SCAN_A, SCAN_B} and by_scan[SCAN_A]["stage"] == "llm_analysis" and by_scan[SCAN_B]["stage"] == "policy_analysis"
    assert by_scan[SCAN_A]["usage_sink"] is not by_scan[SCAN_B]["usage_sink"]


def test_a_refused_call_keeps_the_usage_the_provider_billed(monkeypatch):
    """A refusal is raised after the provider answered and billed. Its tokens and request id must reach the usage sink, not vanish with the exception."""
    settings = get_settings().model_copy(update={"anthropic_api_key": "k"})
    c = llm_client.ClaudeLLMClient(settings)

    async def create(**kw):
        return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"), content=[], _request_id="req_refused",
                               usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 700, "output_tokens": 9}))

    monkeypatch.setattr(c._client.messages, "create", create)
    sink: dict = {}
    with pytest.raises(LLMOutputValidationError, match="declined"):
        asyncio.run(c.generate_structured(system="s", user="u", schema=ConsentAnalysisResponse, usage_sink=sink))
    assert sink["usage_by_attempt"] == [{"input_tokens": 700, "output_tokens": 9, "request_id": "req_refused"}]


def test_a_claude_answer_carries_its_request_id_beside_its_usage(monkeypatch):
    settings = get_settings().model_copy(update={"anthropic_api_key": "k"})
    c = llm_client.ClaudeLLMClient(settings)

    async def create(**kw):
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text='{"findings": []}')], _request_id="req_ok",
                               usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 10, "output_tokens": 2}))

    monkeypatch.setattr(c._client.messages, "create", create)
    sink: dict = {}
    asyncio.run(c.generate_structured(system="s", user="u", schema=ConsentAnalysisResponse, usage_sink=sink))
    assert sink["usage_by_attempt"][0]["request_id"] == "req_ok"


# ── recording never breaks the call ────────────────────────────────────────────────────────────────────────────────


def test_a_failing_database_write_is_swallowed(monkeypatch):
    monkeypatch.delenv("CONSIVA_DISABLE_USAGE_DB", raising=False)

    async def boom(db, fields):
        raise RuntimeError("database is down")

    monkeypatch.setattr(usage_repository, "insert_event", boom)
    # the session factory is real but points nowhere: any failure must be swallowed
    out = asyncio.run(ut.record_llm_event(provider="claude", model="claude-haiku-5-5", agent="consent", usage_sink={"usage_by_attempt": [CLAUDE_USAGE]}, fallback_meta={}, scan_id=SCAN_A))
    assert out is None


def test_a_malformed_usage_sink_does_not_raise():
    out = asyncio.run(ut.record_llm_event(provider="claude", model="m", agent="consent", usage_sink={"usage_by_attempt": "garbage"}, fallback_meta={}, scan_id=SCAN_A))
    assert out is None or out["input_tokens"] == 0


def test_the_flat_file_goes_where_it_is_told_and_holds_no_text(tmp_path, monkeypatch):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv("CONSIVA_LLM_USAGE_LOG", str(target))
    rec = ut.record_llm_call(provider="claude", model="claude-haiku-5-5", agent="consent", usage_sink={"usage_by_attempt": [CLAUDE_USAGE]}, fallback_meta={}, scan_id=SCAN_A)
    line = json.loads(target.read_text(encoding="utf-8").strip())
    assert line["input_tokens"] == 11841 and line["reasoning_tokens"] == 2304 and line["scan_id"] == SCAN_A and rec["cost_usd"] == line["cost_usd"]
    assert set(line) <= {"ts", "provider", "model", "agent", "scan_id", "request_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                         "reasoning_tokens", "cost_usd", "fallback_triggered"}


def test_the_test_run_does_not_write_to_the_real_usage_log():
    assert os.environ["CONSIVA_DISABLE_USAGE_DB"] == "1"
    assert "consiva-test-llm-usage" in os.environ["CONSIVA_LLM_USAGE_LOG"]
