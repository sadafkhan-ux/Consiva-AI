"""Mocked LLM client tests — master prompt §10: "mock the LLM — no live key needed in
CI." Bypasses NvidiaLLMClient.__init__ (which builds a real AsyncOpenAI client from
settings) via __new__, then injects a fake `_client` directly -- exercising
BaseLLMClient's shared generate_structured()/\u200b_chat() logic, which both NvidiaLLMClient
and GroqLLMClient inherit unchanged."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from pydantic import BaseModel

from app.core.exceptions import LLMOutputValidationError
from app.llm.client import (
    GroqLLMClient,
    NvidiaLLMClient,
    SelfHostedLLMClient,
    generate_structured_with_fallback,
    get_reasoning_llm_client,
    resolve_reasoning_provider_and_model,
)


class _Schema(BaseModel):
    value: str


def _client_with_chat_responses(responses: list[str]) -> NvidiaLLMClient:
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client.provider = "nvidia"
    client.model_name = "test-model"
    client._extra_body = {}
    client._call_timeout = 60.0
    client._max_output_tokens = 4000
    fake_openai = MagicMock()
    responses_iter = iter(responses)

    async def fake_create(**kwargs):
        message = MagicMock(content=next(responses_iter))
        return MagicMock(choices=[MagicMock(message=message)])

    fake_openai.chat.completions.create = AsyncMock(side_effect=fake_create)
    client._client = fake_openai
    return client


async def test_generate_structured_returns_valid_response():
    client = _client_with_chat_responses([json.dumps({"value": "ok"})])
    result = await client.generate_structured(system="sys", user="usr", schema=_Schema)
    assert result.value == "ok"


async def test_generate_structured_retries_on_malformed_json_then_succeeds():
    client = _client_with_chat_responses(["not valid json", json.dumps({"value": "ok"})])
    result = await client.generate_structured(system="sys", user="usr", schema=_Schema, max_attempts=3)
    assert result.value == "ok"
    assert client._client.chat.completions.create.await_count == 2


async def test_chat_does_not_retry_permanent_auth_error():
    """A bad API key (401) will never succeed on retry -- confirms _chat's tenacity
    retry now excludes AuthenticationError (and the other permanent 4xx errors) so it
    fails fast on the first attempt instead of burning ~15s of backoff first."""
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client.provider = "nvidia"
    client.model_name = "test-model"
    client._extra_body = {}
    client._call_timeout = 60.0
    client._max_output_tokens = 4000
    fake_openai = MagicMock()

    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    response = httpx.Response(status_code=401, request=request)
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)

    fake_openai.chat.completions.create = AsyncMock(side_effect=auth_error)
    client._client = fake_openai

    with pytest.raises(openai.AuthenticationError):
        await client._chat([{"role": "user", "content": "hi"}])

    assert fake_openai.chat.completions.create.await_count == 1


async def test_generate_structured_raises_after_exhausting_retries():
    client = _client_with_chat_responses(["not json", "still not json"])
    with pytest.raises(LLMOutputValidationError):
        await client.generate_structured(system="sys", user="usr", schema=_Schema, max_attempts=2)


async def test_generate_structured_retries_on_schema_mismatch():
    client = _client_with_chat_responses([json.dumps({"wrong_field": 1}), json.dumps({"value": "ok"})])
    result = await client.generate_structured(system="sys", user="usr", schema=_Schema, max_attempts=3)
    assert result.value == "ok"


async def test_embed_returns_vectors_and_passes_input_type():
    # The mock's vector length must match the configured dimensions -- embed() now
    # verifies the two agree (the pgvector column is fixed-width, so a silent
    # mismatch would otherwise surface later as a confusing insert/search error).
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client._settings = MagicMock(nvidia_embed_model="test-embed", nvidia_embed_dimensions=3)
    fake_openai = MagicMock()
    fake_openai.embeddings.create = AsyncMock(
        return_value=MagicMock(data=[MagicMock(embedding=[0.1, 0.2, 0.3])])
    )
    client._client = fake_openai

    result = await client.embed(["hello"], input_type="query")

    assert result == [[0.1, 0.2, 0.3]]
    _, kwargs = fake_openai.embeddings.create.call_args
    assert kwargs["extra_body"] == {"input_type": "query", "dimensions": 3}
    assert kwargs["model"] == "test-embed"


async def test_embed_rejects_dimension_mismatch():
    """The new dimension guard: a model returning vectors that don't match the
    configured (and pgvector-column-fixed) width must fail loudly at the source."""
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client._settings = MagicMock(nvidia_embed_model="test-embed", nvidia_embed_dimensions=1024)
    fake_openai = MagicMock()
    fake_openai.embeddings.create = AsyncMock(
        return_value=MagicMock(data=[MagicMock(embedding=[0.1, 0.2, 0.3])])
    )
    client._client = fake_openai

    with pytest.raises(LLMOutputValidationError, match="3 dimensions, configured for 1024"):
        await client.embed(["hello"], input_type="query")


# ---------------------------------------------------------------------------
# Groq provider (added alongside NVIDIA, not replacing it)
# ---------------------------------------------------------------------------


def _settings(**overrides):
    base = {
        "nvidia_api_key": "nvapi-test", "nvidia_api_base_url": "https://integrate.api.nvidia.com/v1",
        "nvidia_llm_model": "nvidia-test-model", "nvidia_embed_model": "nvidia-embed-test", "nvidia_embed_dimensions": 3,
        "llm_provider": "nvidia", "groq_api_key": None, "groq_api_base_url": "https://api.groq.com/openai/v1", "groq_model": None,
        "llm_primary_base_url": None, "llm_primary_model": None, "llm_primary_api_key": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def test_get_reasoning_llm_client_defaults_to_nvidia():
    """Default (no LLM_PROVIDER set) must keep existing behavior exactly -- NVIDIA,
    unchanged, for every deployment/test that predates Groq support."""
    client = get_reasoning_llm_client(_settings())
    assert isinstance(client, NvidiaLLMClient)
    assert client.provider == "nvidia"
    assert client.model_name == "nvidia-test-model"


def test_get_reasoning_llm_client_selects_groq_when_configured():
    client = get_reasoning_llm_client(_settings(llm_provider="groq", groq_api_key="gsk_test", groq_model="openai/gpt-oss-120b"))
    assert isinstance(client, GroqLLMClient)
    assert client.provider == "groq"
    assert client.model_name == "openai/gpt-oss-120b"


def test_groq_client_requires_api_key_and_model():
    """A clear, real config error -- not a silent fallback to NVIDIA and not an
    obscure AttributeError three calls later -- when LLM_PROVIDER=groq is selected
    without the two vars it actually needs."""
    with pytest.raises(LLMOutputValidationError, match="GROQ_API_KEY and GROQ_MODEL"):
        GroqLLMClient(_settings(llm_provider="groq", groq_api_key=None, groq_model=None))


def test_resolve_reasoning_provider_and_model_nvidia():
    assert resolve_reasoning_provider_and_model(_settings()) == ("nvidia", "nvidia-test-model")


def test_resolve_reasoning_provider_and_model_groq():
    assert resolve_reasoning_provider_and_model(
        _settings(llm_provider="groq", groq_model="openai/gpt-oss-120b")
    ) == ("groq", "openai/gpt-oss-120b")


def _groq_client_with_chat_responses(responses: list[str]) -> GroqLLMClient:
    client = GroqLLMClient.__new__(GroqLLMClient)
    client.provider = "groq"
    client.model_name = "openai/gpt-oss-120b"
    client._extra_body = {}
    client._call_timeout = 60.0
    client._max_output_tokens = 4000
    fake_openai = MagicMock()
    responses_iter = iter(responses)

    async def fake_create(**kwargs):
        message = MagicMock(content=next(responses_iter))
        return MagicMock(choices=[MagicMock(message=message)])

    fake_openai.chat.completions.create = AsyncMock(side_effect=fake_create)
    client._client = fake_openai
    return client


async def test_groq_generate_structured_uses_same_shared_contract_as_nvidia():
    """Groq inherits generate_structured()/\u200b_chat() from BaseLLMClient unchanged --
    same retry-on-invalid-json behavior NVIDIA already has, proven here against a
    Groq-flavored client instance, not a separate reimplementation."""
    client = _groq_client_with_chat_responses(["not valid json", json.dumps({"value": "ok"})])
    result = await client.generate_structured(system="sys", user="usr", schema=_Schema, max_attempts=3)
    assert result.value == "ok"
    assert client._client.chat.completions.create.await_count == 2


async def test_groq_embed_raises_not_implemented():
    """Confirmed live against Groq's real /v1/models catalog: no embedding model
    exists at all. embed() must fail loudly, not silently return garbage or route
    through NVIDIA without saying so."""
    client = GroqLLMClient.__new__(GroqLLMClient)
    client.provider = "groq"
    with pytest.raises(NotImplementedError, match="no text-embeddings endpoint"):
        await client.embed(["hello"], input_type="query")


# ---------------------------------------------------------------------------
# Self-hosted primary provider + primary/fallback orchestration
# ---------------------------------------------------------------------------

_PRIMARY_SETTINGS = {
    "llm_primary_base_url": "https://gemma4-server.gignati.com/v1",
    "llm_primary_model": "/home/gignaati/dbeaver/MODELS_LLAMA_CPP/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-Q4_K_M.gguf",
}


def test_self_hosted_client_requires_base_url_and_model():
    with pytest.raises(LLMOutputValidationError, match="LLM_PRIMARY_BASE_URL"):
        SelfHostedLLMClient(_settings())


def test_self_hosted_client_reserves_less_output_context():
    """The self-hosted server's context pool (n_ctx=30208 shared across 4 slots, per
    its own live /props and /slots) is small enough that reserved-but-unused output
    tokens matter -- llama.cpp reserves prompt + max_tokens up front. 3000 keeps ~50%
    headroom over the largest completion ever recorded in this project (1,995 across
    47 real attempts) while handing ~1000 tokens back to the prompt."""
    client = SelfHostedLLMClient(_settings(**_PRIMARY_SETTINGS))
    assert client._max_output_tokens == 3000

    # The large-context hosted providers keep the original, roomier budget.
    assert NvidiaLLMClient(_settings())._max_output_tokens == 4000


async def test_chat_sends_the_clients_configured_output_budget():
    client = _client_with_chat_responses([json.dumps({"value": "ok"})])
    client._max_output_tokens = 3000

    await client._chat([{"role": "user", "content": "hi"}])

    _, kwargs = client._client.chat.completions.create.call_args
    assert kwargs["max_tokens"] == 3000


def test_self_hosted_client_sends_qwen3_thinking_off_kwarg():
    """The exact, live-verified mechanism for this server/model family --
    confirmed by reading its real chat_template AND by A/B testing that a
    top-level `enable_thinking` field is silently ignored while
    chat_template_kwargs.enable_thinking is genuinely honored (reasoning_content
    disappears, finish_reason changes from "length" to "stop")."""
    client = SelfHostedLLMClient(_settings(**_PRIMARY_SETTINGS))
    assert client.provider == "self_hosted"
    assert client.model_name == _PRIMARY_SETTINGS["llm_primary_model"]
    assert client._extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_resolve_reasoning_provider_and_model_prefers_primary():
    assert resolve_reasoning_provider_and_model(_settings(**_PRIMARY_SETTINGS)) == (
        "self_hosted", _PRIMARY_SETTINGS["llm_primary_model"],
    )


async def test_fallback_not_called_when_primary_succeeds(monkeypatch):
    """Core requirement: 'do not call both providers unnecessarily' -- the fallback
    client must never even be constructed when the primary answers successfully."""
    fake_primary = MagicMock()
    fake_primary.provider = "self_hosted"
    fake_primary.model_name = _PRIMARY_SETTINGS["llm_primary_model"]
    fake_primary.generate_structured = AsyncMock(return_value=_Schema(value="ok"))
    monkeypatch.setattr("app.llm.client.SelfHostedLLMClient", MagicMock(return_value=fake_primary))

    fallback_ctor = MagicMock()
    monkeypatch.setattr("app.llm.client.get_reasoning_llm_client", fallback_ctor)

    result, meta = await generate_structured_with_fallback(
        system="sys", user="usr", schema=_Schema, settings=_settings(**_PRIMARY_SETTINGS),
    )

    assert result.value == "ok"
    assert meta["primary_status"] == "success"
    assert meta["fallback_triggered"] is False
    assert meta["primary_provider"] == "self_hosted"
    fallback_ctor.assert_not_called()  # the whole point of primary/fallback


async def test_fallback_triggered_on_primary_failure(monkeypatch):
    """The real-world case this exists for: primary times out/errors, NVIDIA (the
    configured fallback) picks it up and still produces a valid result."""
    fake_primary = MagicMock()
    fake_primary.provider = "self_hosted"
    fake_primary.model_name = _PRIMARY_SETTINGS["llm_primary_model"]
    fake_primary.generate_structured = AsyncMock(side_effect=TimeoutError("Request timed out."))
    monkeypatch.setattr("app.llm.client.SelfHostedLLMClient", MagicMock(return_value=fake_primary))

    fake_fallback = MagicMock()
    fake_fallback.provider = "nvidia"
    fake_fallback.model_name = "nvidia-test-model"
    fake_fallback.generate_structured = AsyncMock(return_value=_Schema(value="ok"))
    monkeypatch.setattr("app.llm.client.get_reasoning_llm_client", MagicMock(return_value=fake_fallback))

    result, meta = await generate_structured_with_fallback(
        system="sys", user="usr", schema=_Schema, settings=_settings(**_PRIMARY_SETTINGS),
    )

    assert result.value == "ok"
    assert meta["primary_status"] == "failed"
    assert meta["fallback_triggered"] is True
    assert "TimeoutError" in meta["fallback_reason"]
    assert meta["fallback_provider"] == "nvidia"
    fake_fallback.generate_structured.assert_awaited_once()


# ---------------------------------------------------------------------------
# Shared wall-clock deadline is genuinely enforced (not multiplied per provider)
# ---------------------------------------------------------------------------


async def test_chat_caps_request_timeout_to_remaining_deadline():
    """Regression test for a real incident: one llm_analysis stage ran 907s against a
    configured 480s deadline. _chat used to send every request with a fixed 60s
    timeout and a fixed 3-attempt retry regardless of how much budget was left, so
    primary-then-fallback stacked their retries. Each request must now be capped to
    the remaining budget when that is tighter than the client's own timeout."""
    client = _client_with_chat_responses([json.dumps({"value": "ok"})])
    client._call_timeout = 60.0
    client._max_output_tokens = 4000

    await client._chat([{"role": "user", "content": "hi"}], deadline_epoch=time.time() + 5)

    _, kwargs = client._client.chat.completions.create.call_args
    assert kwargs["timeout"] <= 5, "request timeout must shrink to the remaining budget"


async def test_chat_uses_full_call_timeout_when_no_deadline_given():
    """Backward compatibility: with no deadline_epoch (every pre-existing call site),
    behavior is unchanged -- the client's own configured timeout is used as-is."""
    client = _client_with_chat_responses([json.dumps({"value": "ok"})])
    client._call_timeout = 60.0
    client._max_output_tokens = 4000

    await client._chat([{"role": "user", "content": "hi"}])

    _, kwargs = client._client.chat.completions.create.call_args
    assert kwargs["timeout"] == 60.0


async def test_chat_refuses_to_send_once_deadline_already_passed():
    """The fallback provider must not start a fresh 3x60s retry cycle after the
    primary already consumed the whole shared budget -- that was exactly how the
    real 907s stage happened."""
    client = _client_with_chat_responses([json.dumps({"value": "ok"})])
    client._call_timeout = 60.0
    client._max_output_tokens = 4000

    with pytest.raises(LLMOutputValidationError, match="deadline already exceeded"):
        await client._chat([{"role": "user", "content": "hi"}], deadline_epoch=time.time() - 1)

    client._client.chat.completions.create.assert_not_awaited()


async def test_chat_stops_retrying_once_deadline_elapses():
    """A failing provider must stop retrying when the shared budget runs out, rather
    than always burning its full 3 attempts."""
    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    client = NvidiaLLMClient.__new__(NvidiaLLMClient)
    client.provider = "nvidia"
    client.model_name = "test-model"
    client._extra_body = {}
    client._call_timeout = 60.0
    client._max_output_tokens = 4000
    fake_openai = MagicMock()
    fake_openai.chat.completions.create = AsyncMock(side_effect=openai.APIConnectionError(request=req))
    client._client = fake_openai

    with pytest.raises(openai.APIConnectionError):
        await client._chat([{"role": "user", "content": "hi"}], deadline_epoch=time.time() + 0.5)

    # Without the deadline this would always be exactly 3; with only 0.5s of budget
    # the retry loop must give up early instead.
    assert fake_openai.chat.completions.create.await_count < 3


async def test_fallback_shares_one_deadline_with_primary(monkeypatch):
    """End-to-end contract: the SAME deadline_epoch object reaches both providers, so
    a fallback attempt only ever gets the budget the primary did not spend."""
    seen_deadlines = []

    fake_primary = MagicMock()
    fake_primary.provider = "self_hosted"
    fake_primary.model_name = _PRIMARY_SETTINGS["llm_primary_model"]

    async def primary_call(**kwargs):
        seen_deadlines.append(kwargs["deadline_epoch"])
        raise TimeoutError("Request timed out.")

    fake_primary.generate_structured = AsyncMock(side_effect=primary_call)
    monkeypatch.setattr("app.llm.client.SelfHostedLLMClient", MagicMock(return_value=fake_primary))

    fake_fallback = MagicMock()
    fake_fallback.provider = "nvidia"
    fake_fallback.model_name = "nvidia-test-model"

    async def fallback_call(**kwargs):
        seen_deadlines.append(kwargs["deadline_epoch"])
        return _Schema(value="ok")

    fake_fallback.generate_structured = AsyncMock(side_effect=fallback_call)
    monkeypatch.setattr("app.llm.client.get_reasoning_llm_client", MagicMock(return_value=fake_fallback))

    deadline = time.time() + 30
    result, meta = await generate_structured_with_fallback(
        system="sys", user="usr", schema=_Schema, settings=_settings(**_PRIMARY_SETTINGS),
        deadline_epoch=deadline,
    )

    assert result.value == "ok"
    assert meta["fallback_triggered"] is True
    assert seen_deadlines == [deadline, deadline], "both providers must share one budget, not get one each"


async def test_no_primary_configured_goes_straight_to_fallback(monkeypatch):
    """Backward compatibility: with no LLM_PRIMARY_* set, behavior is identical to
    before primary/fallback existed -- straight to settings.llm_provider's client,
    primary never even constructed."""
    primary_ctor = MagicMock()
    monkeypatch.setattr("app.llm.client.SelfHostedLLMClient", primary_ctor)

    fake_fallback = MagicMock()
    fake_fallback.provider = "nvidia"
    fake_fallback.model_name = "nvidia-test-model"
    fake_fallback.generate_structured = AsyncMock(return_value=_Schema(value="ok"))
    monkeypatch.setattr("app.llm.client.get_reasoning_llm_client", MagicMock(return_value=fake_fallback))

    result, meta = await generate_structured_with_fallback(system="sys", user="usr", schema=_Schema, settings=_settings())

    assert result.value == "ok"
    assert meta["primary_attempted"] is False
    assert meta["fallback_triggered"] is False
    primary_ctor.assert_not_called()
