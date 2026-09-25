"""Provider-agnostic LLM client layer for the Consent Agent.

    BaseLLMClient (shared: retry loop, deadline budget, schema validation,
                   retry-on-invalid-output feedback, structured-output contract)
        |-- NvidiaLLMClient  (chat + embeddings -- embeddings are NVIDIA-only, see below)
        `-- GroqLLMClient    (chat only)

Both concrete clients wrap the SAME generic `openai.AsyncOpenAI` transport pointed at
a different OpenAI-compatible base_url (verified against current docs:
NVIDIA NIM -> https://integrate.api.nvidia.com/v1, Groq -> https://api.groq.com/openai/v1)
-- no provider-specific SDK, no provider-specific business logic anywhere outside this
file. `get_reasoning_llm_client()` is the ONE place that reads `settings.llm_provider`
and picks a concrete class; every caller (agents/consent_agent/nodes/llm_reasoning.py)
only ever calls the shared `generate_structured()` contract.

Embeddings stay NVIDIA-only regardless of `LLM_PROVIDER`: confirmed live against
Groq's real /v1/models endpoint that its catalog is chat/audio-only (no embedding
model at all) -- RAG retrieval/ingestion (app/rag/retriever.py, app/rag/ingest.py)
already instantiate NvidiaLLMClient directly and are deliberately left untouched, per
"keep the current RAG implementation unchanged."
"""

import functools
import json
import logging
import time
from typing import Literal, TypeVar

from openai import (
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_not_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.core.exceptions import LLMOutputValidationError
from app.llm.prompts import RETRY_SUFFIX

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# A bad API key / malformed request / unknown model is a permanent failure that will
# never succeed on retry -- retrying it 3x with exponential backoff just adds up to
# ~15s of pure waste before the real error finally surfaces. Only these are excluded;
# everything else (connection errors, timeouts, rate limits, 5xx) still retries.
# These are all generic `openai` SDK exception types -- provider-agnostic already,
# since both NVIDIA and Groq are driven through the same SDK.
_NON_RETRYABLE_ERRORS = (
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
    ConflictError,
)

# Bounds on what a schema-repair attempt carries forward. A pydantic ValidationError
# over a large schema can itself run to thousands of characters, and the failed
# response is unbounded by definition when the failure IS that it ran too long -- so
# both are excerpted. ~1,200 chars is roughly 300 tokens: enough of the previous
# attempt to show its shape, small enough that three repairs cannot meaningfully move
# the prompt against a 4k context.
_REPAIR_EXCERPT_CHARS = 1_200
_REPAIR_ERROR_CHARS = 600


def _record_chat_retry(provider: str, retry_meta: dict | None, retry_state: RetryCallState) -> None:
    """tenacity before_sleep hook -- records each retried attempt of _chat's OWN
    network-level retry (distinct from generate_structured's schema-validation retry
    loop) into the caller's retry_meta dict, if one was passed. Exists because a slow
    `llm_analysis` stage duration was otherwise indistinguishable between "one
    genuinely slow response" and "several retried attempts stacked inside one
    stage" -- a real gap surfaced during a latency audit, not decorative telemetry.

    Bound via functools.partial (provider/retry_meta as explicit closure args) rather
    than read off retry_state.args/kwargs, because _chat now builds its AsyncRetrying
    instance manually (see _chat's docstring for why) instead of using the `@retry`
    decorator -- manual retrying never populates retry_state.args/kwargs from the
    enclosing call, so those would silently read as None here otherwise."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning("%s chat request failed, retrying (attempt %d): %s", provider, retry_state.attempt_number, exc)
    if retry_meta is not None:
        retry_meta.setdefault("chat_retries", []).append({
            "attempt": retry_state.attempt_number,
            "error": f"{type(exc).__name__}: {exc}" if exc else None,
            "seconds_since_start": round(retry_state.seconds_since_start, 2),
        })


def _strict_json_schema(schema: dict) -> dict:
    """Add `additionalProperties: false` to every object in a JSON schema.

    Required by the OpenAI strict structured-outputs spec, which Groq enforces and
    llama.cpp does not. Sending the schema as pydantic emits it got:

        400 invalid JSON schema for response_format 'ConsentAnalysisResponse':
        /$defs/ConsentFindingLLM: `additionalProperties:false` must be set on every object

    Applied here rather than as `extra="forbid"` on the models, because this changes
    only what goes on the wire. Setting it on the pydantic models would also make them
    REJECT a response carrying an unexpected field, turning something currently
    harmless into a failed validation and an extra repair round trip.

    Recursive over $defs, properties, items and the composition keywords, so nested
    finding objects are covered too -- the error above is about a $def, not the root.
    """
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items()}
    if out.get("type") == "object" or "properties" in out:
        out.setdefault("additionalProperties", False)
    for key in ("properties", "$defs", "definitions", "patternProperties"):
        if isinstance(out.get(key), dict):
            out[key] = {k: _strict_json_schema(v) for k, v in out[key].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _strict_json_schema(out["items"])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(out.get(key), list):
            out[key] = [_strict_json_schema(v) for v in out[key]]
    return out


class BaseLLMClient:
    """Shared OpenAI-compatible chat implementation -- the "LLM Provider Interface".
    Every concrete provider sets `provider`/`model_name` and constructs the transport
    via `_init_transport`; nothing else differs between providers at this layer."""

    provider: str
    model_name: str
    # Class-level defaults so the transport-shaped attributes always exist even when a
    # subclass instance is built without _init_transport (the __new__ + injected fake
    # `_client` pattern the mocked tests use). _init_transport overrides all of them.
    _call_timeout: float = 60.0
    _max_output_tokens: int = 4000
    _supports_grammar: bool = False
    _extra_body: dict = {}  # noqa: RUF012 -- read-only default, always replaced per instance

    def _init_transport(
        self, *, base_url: str, api_key: str, model: str, extra_body: dict | None = None,
        timeout: float = 60.0, max_output_tokens: int = 4000, supports_grammar: bool = False,
    ) -> None:
        self.model_name = model
        self._extra_body = extra_body or {}
        # The per-call ceiling _chat falls back to when no shared deadline_epoch is
        # given, and the upper bound it never exceeds even when one is (see _chat).
        self._call_timeout = timeout
        # Ceiling on generated tokens. Measured across 47 real llm_analysis attempts in
        # this project's own history, the largest completion ever produced was 1,995
        # tokens -- so 4000 is roughly 2x headroom.
        #
        # This does NOT reserve context away from the prompt on the self-hosted server:
        # that server's own refusal names the prompt alone against n_ctx (see
        # SelfHostedLLMClient), so the prompt has to fit by itself and the output takes
        # what is left. A context-constrained provider still lowers this, to stop a
        # generation running on until the remaining context is exhausted.
        self._max_output_tokens = max_output_tokens
        # Grammar-constrained decoding (response_format=json_schema) makes
        # schema-invalid output structurally impossible, which removes entire
        # generate_structured repair attempts -- each of which costs a full
        # prefill+decode round trip. Confirmed working live on the self-hosted
        # llama.cpp server; left OFF for providers where it has not been verified,
        # since an unsupported response_format is a 400, not a graceful degrade.
        self._supports_grammar = supports_grammar
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout,
            # A clear, honest, self-identifying User-Agent -- normal practice for a
            # backend service calling an API, not stealth/evasion (this does NOT
            # touch the openai SDK's own x-stainless-* diagnostic headers, which
            # stay exactly as the SDK sets them; see SelfHostedLLMClient's docstring
            # for the real, confirmed-live case where a WAF in front of a
            # self-hosted server blocks on those SDK headers specifically -- the fix
            # for that lives on the server/WAF side, not by disguising this client).
            default_headers={"User-Agent": "Consiva-ConsentAgent/1.0 (+internal LLM client)"},
        )

    def _response_format(self, schema: type[BaseModel] | None) -> dict:
        """json_schema (grammar-enforced) where the provider is known to support it,
        plain json_object everywhere else -- both keep the existing "valid JSON only"
        contract, the former just also guarantees the shape."""
        if schema is not None and self._supports_grammar:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": _strict_json_schema(schema.model_json_schema()),
                    "strict": True,
                },
            }
        return {"type": "json_object"}

    async def _chat(
        self, messages: list[dict], *, retry_meta: dict | None = None, deadline_epoch: float | None = None,
        schema: type[BaseModel] | None = None,
    ) -> tuple[str, dict]:
        """Bounded by BOTH a fixed per-call timeout (self._call_timeout) and, when
        `deadline_epoch` is given, the shared wall-clock budget passed down from
        generate_structured -- neither the per-call timeout nor the retry loop's own
        3 attempts can run past whatever time is actually left on that budget.

        This replaced a static `@retry(stop=stop_after_attempt(3), ...)` decorator
        after a real, live bug: with a fixed 3-attempt/60s-timeout retry regardless of
        remaining budget, a single _chat() call could burn up to ~3x its own timeout,
        and calling it again for a fallback provider after the primary's retries were
        exhausted ADDED another ~3x on top -- exactly the "multiplied timeouts" the
        primary/fallback design is required not to do. Observed for real: one
        llm_analysis stage ran 907s against a configured 480s deadline (self-hosted
        primary hit genuine "context size exceeded" errors, NVIDIA fallback was
        independently slow that same run, and neither retry loop knew about the
        other's spent time). Building the retry manually per-call lets both the
        per-call timeout and the stop condition shrink to whatever budget is
        actually left, so total elapsed time across primary+fallback stays bounded
        by deadline_epoch (plus at most one in-flight call's own timeout), matching
        what generate_structured's docstring already promises callers."""
        if deadline_epoch is not None:
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                raise LLMOutputValidationError(
                    "LLM wall-clock deadline already exceeded before this request could be sent."
                )
            call_timeout = min(self._call_timeout, remaining)
            stop_condition = stop_after_attempt(3) | stop_after_delay(remaining)
        else:
            call_timeout = self._call_timeout
            stop_condition = stop_after_attempt(3)

        retrying = AsyncRetrying(
            reraise=True,
            stop=stop_condition,
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_not_exception_type(_NON_RETRYABLE_ERRORS),
            before_sleep=functools.partial(_record_chat_retry, self.provider, retry_meta),
        )
        async for attempt in retrying:
            with attempt:
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    response_format=self._response_format(schema),
                    temperature=0.1,
                    max_tokens=self._max_output_tokens,
                    extra_body=self._extra_body,
                    timeout=call_timeout,
                )
        usage = response.usage.model_dump() if response.usage else {}
        return response.choices[0].message.content or "", usage

    async def generate_structured(
        self, *, system: str, user: str, schema: type[T], max_attempts: int = 3,
        usage_sink: dict | None = None, deadline_epoch: float | None = None,
    ) -> T:
        """`usage_sink`, if given, is populated with `{"attempts": N, "usage_by_attempt": [...]}`
        -- token-level cost data for latency/optimization analysis, kept out of the return
        type so callers that don't need it aren't forced to unpack a tuple.

        `deadline_epoch` (epoch seconds), if given, is a shared wall-clock budget that is
        now genuinely enforced at every layer: no new attempt starts past it, AND it is
        passed down into _chat, which caps both each individual request's timeout and its
        own retry loop to whatever budget is left. This is the caller-owned cross-layer
        budget -- this loop's max_attempts and _chat's retries are each individually
        bounded but multiply together without it (see _chat for the real 907s-vs-480s
        incident that motivated pushing the deadline all the way down)."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        usage_by_attempt: list[dict] = []
        retry_meta: dict = {}

        for attempt in range(1, max_attempts + 1):
            if deadline_epoch is not None and time.time() > deadline_epoch:
                raise LLMOutputValidationError(
                    f"LLM wall-clock deadline exceeded before attempt {attempt}/{max_attempts} "
                    f"(shared budget across all retry layers); last error: {last_error}"
                )
            raw, usage = await self._chat(
                messages, retry_meta=retry_meta, deadline_epoch=deadline_epoch, schema=schema
            )
            usage_by_attempt.append(usage)
            try:
                parsed = schema.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as exc:
                logger.warning("LLM output failed validation (attempt %d/%d): %s", attempt, max_attempts, exc)
                last_error = exc
                # Rebuild the conversation instead of growing it.
                #
                # This used to append the failed response AND the correction to
                # `messages` and carry both into every later attempt, so each repair
                # made the prompt permanently bigger. On a context-constrained server
                # that turns one recoverable failure into an unrecoverable one, and it
                # was observed doing exactly that on a live projectflow scan: the first
                # attempt came back as truncated JSON ("EOF while parsing at line 37"),
                # the retry re-sent that truncated blob plus the error, and the request
                # grew from ~3,300 to 4,204 tokens against an n_ctx of 4,096 --
                # "exceeds the available context size", primary abandoned, 548 seconds
                # spent failing over to a provider that then timed out.
                #
                # Truncation is the case that matters here: the response was too LONG
                # to finish, so feeding it back in full is both the least useful
                # context and the most expensive. Keeping a bounded excerpt preserves
                # what the repair actually needs (the shape the model was producing)
                # while making prompt growth constant rather than cumulative.
                excerpt = raw[:_REPAIR_EXCERPT_CHARS]
                if len(raw) > _REPAIR_EXCERPT_CHARS:
                    excerpt += "\n... (truncated; your previous response did not finish)"
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": excerpt},
                    {"role": "user", "content": RETRY_SUFFIX.format(error=str(exc)[:_REPAIR_ERROR_CHARS])},
                ]
                continue
            if usage_sink is not None:
                usage_sink["attempts"] = attempt
                usage_sink["usage_by_attempt"] = usage_by_attempt
                usage_sink["chat_retries"] = retry_meta.get("chat_retries", [])
            return parsed

        if usage_sink is not None:
            usage_sink["attempts"] = max_attempts
            usage_sink["usage_by_attempt"] = usage_by_attempt
            usage_sink["chat_retries"] = retry_meta.get("chat_retries", [])
        raise LLMOutputValidationError(
            f"LLM failed to produce valid {schema.__name__} after {max_attempts} attempts: {last_error}"
        )


class NvidiaLLMClient(BaseLLMClient):
    """NVIDIA NIM's OpenAI-compatible API (verified against current NVIDIA docs:
    base_url=https://integrate.api.nvidia.com/v1 for both chat completions and
    /v1/embeddings). The ONLY provider that implements `embed()` -- see module
    docstring for why."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self.provider = "nvidia"
        self._init_transport(
            base_url=self._settings.nvidia_api_base_url,
            api_key=self._settings.nvidia_api_key,
            model=self._settings.nvidia_llm_model,
            # nemotron-3.5-lightning is a reasoning model: with chat_template_kwargs.thinking
            # left on, it burns thousands of tokens on a hidden reasoning trace before ever
            # emitting the answer (measured: still mid-thought at 1500 completion tokens,
            # ~70 tok/s free-form / ~21 tok/s under json_object mode) -- an unbounded call
            # can run several minutes. 60s is a hard ceiling once thinking is disabled below
            # (measured ~15-20s for this prompt size). Nemotron-specific -- not sent to Groq.
            extra_body={"chat_template_kwargs": {"thinking": False}},
            timeout=60.0,
        )

    async def embed(self, texts: list[str], *, input_type: Literal["query", "passage"]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=self._settings.nvidia_embed_model,
            input=texts,
            # `dimensions` matters for Matryoshka-capable models (e.g. llama-3.2-nv-embedqa-1b-v2,
            # default 2048) — pins output to NVIDIA_EMBED_DIMENSIONS so it matches the
            # pgvector column's fixed width regardless of the model's own default.
            extra_body={"input_type": input_type, "dimensions": self._settings.nvidia_embed_dimensions},
        )
        embeddings = [item.embedding for item in response.data]
        # The pgvector column is vector(1024) hardcoded in the SQL migration while this
        # dimension is env-configured -- a silent mismatch (model ignoring `dimensions`,
        # or .env changed without a schema migration) would otherwise surface later as a
        # confusing insert/search error. Fail here, at the source, with a clear message.
        for emb in embeddings:
            if len(emb) != self._settings.nvidia_embed_dimensions:
                raise LLMOutputValidationError(
                    f"Embedding model returned {len(emb)} dimensions, configured for "
                    f"{self._settings.nvidia_embed_dimensions} (and the pgvector column is fixed-width) -- "
                    "model/config/schema are out of sync."
                )
        return embeddings


class GroqLLMClient(BaseLLMClient):
    """Groq's OpenAI-compatible API (verified live against
    https://api.groq.com/openai/v1/models with a real key: chat/audio models only,
    no embedding model in the catalog at all). Reasoning/structured-output only --
    `embed()` raises rather than silently doing something wrong, since no RAG call
    site should ever reach it (retrieve_rag.py/ingest.py always use NvidiaLLMClient
    directly for embeddings, regardless of LLM_PROVIDER)."""

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        if not settings.groq_api_key or not settings.groq_model:
            raise LLMOutputValidationError(
                "LLM_PROVIDER=groq requires GROQ_API_KEY and GROQ_MODEL to both be set in .env."
            )
        self.provider = "groq"
        self._init_transport(
            base_url=settings.groq_api_base_url,
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            timeout=60.0,
            # Verified live against openai/gpt-oss-120b on all three of this project's
            # real analysis prompts (2,954 / 3,600 / 6,294 tokens): response_format
            # json_schema is accepted and returned first-try-valid output every time,
            # with zero ungrounded citations. That removes whole generate_structured
            # repair attempts, each of which is a full extra round trip AND -- on a
            # token-per-minute quota -- a second full charge for the same prompt.
            #
            # Checked rather than assumed: the SAME call against openai/gpt-oss-20b
            # returned "400 Failed to validate JSON" on one of the three, so this is a
            # property of the configured model, not of Groq. If GROQ_MODEL is changed,
            # re-verify before trusting this flag -- an unsupported response_format is
            # a 400, not a graceful degrade.
            supports_grammar=True,
        )

    async def embed(self, texts: list[str], *, input_type: Literal["query", "passage"]) -> list[list[float]]:
        raise NotImplementedError(
            "Groq has no text-embeddings endpoint (confirmed live against its own /v1/models catalog) -- "
            "RAG embedding/retrieval always uses NvidiaLLMClient directly, independent of LLM_PROVIDER."
        )


class SelfHostedLLMClient(BaseLLMClient):
    """A self-hosted OpenAI-compatible inference server (e.g. llama.cpp's own
    `server` binary) as the PRIMARY reasoning provider -- settings.llm_provider
    (NVIDIA/Groq) becomes the FALLBACK, used only if this one genuinely fails.

    Confirmed live against a real running instance, not assumed:
      - /v1/models and /props both respond (llama.cpp server's actual API surface;
        system_fingerprint in every response is a real llama.cpp build id).
      - /v1/chat/completions is OpenAI-compatible, including JSON mode
        (response_format={"type":"json_object"}).
      - Qwen3's "thinking" toggle is a DIFFERENT mechanism than NVIDIA Nemotron's:
        confirmed by reading the server's own live chat_template (fetched via
        /props) AND by empirical A/B testing -- a top-level `enable_thinking` field
        is silently ignored (identical output either way), but
        `chat_template_kwargs: {"enable_thinking": false}` genuinely suppresses it
        (reasoning_content disappears, finish_reason goes from "length" to "stop").
      - No embeddings capability (`/v1/models` reports `"capabilities":["completion"]`
        only for this model) -- same as Groq, embed() always stays NVIDIA-only.
    """

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        if not settings.llm_primary_base_url or not settings.llm_primary_model:
            raise LLMOutputValidationError(
                "A primary LLM client was requested but LLM_PRIMARY_BASE_URL and/or "
                "LLM_PRIMARY_MODEL are not set in .env."
            )
        self.provider = "self_hosted"
        self._init_transport(
            base_url=settings.llm_primary_base_url,
            # The openai SDK requires a non-empty api_key string even when the
            # server enforces no auth at all (confirmed live: this server accepts
            # every request tested with no Authorization header) -- a harmless
            # placeholder, sent as a normal Bearer token, never a fake identity.
            api_key=settings.llm_primary_api_key or "not-required",
            model=settings.llm_primary_model,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            timeout=60.0,
            # The server has been RECONFIGURED since this was last set: it reported
            # n_ctx=30208 across total_slots=4, and live /props today reports
            # n_ctx=4096 across total_slots=8. Anything tuned to the old number is
            # now wrong by 7x.
            #
            # Measured, because the obvious assumption is wrong: this server does NOT
            # reject on prompt + max_tokens. Its refusal names the prompt alone
            # ("request (6622 tokens) exceeds the available context size (4096)") for a
            # prompt independently measured at 6,622 tokens with max_tokens=900. So
            # lowering this does not buy prompt headroom -- what it buys is a generation
            # that stops at a sane length instead of running until the context runs out.
            # The prompt must fit in n_ctx on its own, and the output then takes
            # whatever remains, which is why a 3,978-token prompt came back truncated
            # at 168 tokens while a 3,282-token one completed cleanly in 375.
            #
            # 1800, measured rather than guessed. Live completions on the real schema
            # with thinking disabled: 377 and 396 tokens for 2 findings, i.e. ~190
            # tokens per finding, so 1800 covers roughly 9 findings. The largest
            # completion in this project's history is 1,995, from a run that still had
            # the model's reasoning phase enabled; `enable_thinking: False` below is
            # what makes that no longer representative (the same prompt measured 816
            # output tokens with thinking on and 396 with it off, and only the second
            # produced valid JSON -- the first spent its whole budget thinking and was
            # cut off mid-thought).
            #
            # None of this makes a large site fit. With n_ctx=4096 the prompt alone
            # must clear the bar, so the real ceiling is ~3,700 prompt tokens if the
            # output is to have room to finish. Raising the server's -c is the fix for
            # heavy sites; this value is only the ceiling to raise if findings ever
            # come back truncated on a site that otherwise fits.
            max_output_tokens=1800,
            # Verified live against this server: response_format json_schema is
            # accepted and returns first-try-valid output (A/B tested against plain
            # json_object on the real ConsentAnalysisResponse schema).
            supports_grammar=True,
        )

    async def embed(self, texts: list[str], *, input_type: Literal["query", "passage"]) -> list[list[float]]:
        raise NotImplementedError(
            "The configured self-hosted model has no embeddings capability (confirmed live via its "
            "own /v1/models: capabilities=['completion'] only) -- RAG embedding/retrieval always uses "
            "NvidiaLLMClient directly, independent of the primary/fallback reasoning provider."
        )


def resolve_reasoning_provider_and_model(settings: Settings | None = None) -> tuple[str, str]:
    """Pure, side-effect-free lookup of (provider, model) for the CURRENTLY configured
    reasoning provider -- used to stamp agent_runs.llm_provider/llm_model up front
    (before the graph runs), without constructing a real network client just to read
    two config values. Reflects whichever provider would be tried FIRST (the primary,
    if configured) -- llm_reasoning.py overwrites this afterwards with whichever
    provider actually answered, in case a fallback was triggered."""
    settings = settings or get_settings()
    if settings.llm_primary_base_url and settings.llm_primary_model:
        return "self_hosted", settings.llm_primary_model
    if settings.llm_provider == "groq":
        if not settings.groq_model:
            raise LLMOutputValidationError("LLM_PROVIDER=groq requires GROQ_MODEL to be set in .env.")
        return "groq", settings.groq_model
    return "nvidia", settings.nvidia_llm_model


def get_reasoning_llm_client(settings: Settings | None = None) -> BaseLLMClient:
    """The ONE place `settings.llm_provider` is read to pick a concrete FALLBACK
    client (or the sole client, if no primary is configured) for the Consent
    Agent's reasoning/structured-output step. Everything downstream (prompt
    builder, schemas, citation validator, retry/error handling, audit metadata) only
    ever talks to the shared `BaseLLMClient.generate_structured()` contract."""
    settings = settings or get_settings()
    if settings.llm_provider == "groq":
        return GroqLLMClient(settings)
    return NvidiaLLMClient(settings)


async def generate_structured_with_fallback(
    *, system: str, user: str, schema: type[T], settings: Settings | None = None,
    max_attempts: int = 3, usage_sink: dict | None = None, deadline_epoch: float | None = None,
) -> tuple[T, dict]:
    """Tries the primary self-hosted provider FIRST (if LLM_PRIMARY_* is configured);
    falls back to settings.llm_provider's client (NVIDIA by default) ONLY if the
    primary genuinely fails -- never calls both when the primary succeeds. Both
    attempts share the SAME deadline_epoch (no timeout multiplication: a fallback
    attempt only gets whatever wall-clock budget the primary didn't use).

    Returns (parsed_response, fallback_meta) -- fallback_meta is the exact
    provider/model/status/reason data section 11 asks for, meant to be merged
    straight into the caller's stage metadata dict."""
    settings = settings or get_settings()
    fallback_meta: dict = {
        "primary_attempted": False, "primary_provider": None, "primary_model": None,
        "primary_status": None, "fallback_triggered": False, "fallback_reason": None,
        "fallback_provider": None, "fallback_model": None,
    }

    primary_configured = bool(settings.llm_primary_base_url and settings.llm_primary_model)
    if primary_configured:
        primary_client = SelfHostedLLMClient(settings)
        fallback_meta["primary_attempted"] = True
        fallback_meta["primary_provider"] = primary_client.provider
        fallback_meta["primary_model"] = primary_client.model_name
        try:
            result = await primary_client.generate_structured(
                system=system, user=user, schema=schema, max_attempts=max_attempts,
                usage_sink=usage_sink, deadline_epoch=deadline_epoch,
            )
            fallback_meta["primary_status"] = "success"
            return result, fallback_meta
        except Exception as exc:  # noqa: BLE001 -- ANY exhausted-retry failure from the primary (connection
            # error, timeout, 5xx, or invalid/unparseable output after generate_structured's own internal
            # retries) is exactly the "genuinely failed" condition this fallback exists for.
            fallback_meta["primary_status"] = "failed"
            fallback_meta["fallback_triggered"] = True
            fallback_meta["fallback_reason"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Primary LLM (%s/%s) failed, falling back to %s: %s",
                primary_client.provider, primary_client.model_name, settings.llm_provider, exc,
            )

    fallback_client = get_reasoning_llm_client(settings)
    fallback_meta["fallback_provider"] = fallback_client.provider
    fallback_meta["fallback_model"] = fallback_client.model_name
    result = await fallback_client.generate_structured(
        system=system, user=user, schema=schema, max_attempts=max_attempts,
        usage_sink=usage_sink, deadline_epoch=deadline_epoch,
    )
    return result, fallback_meta
