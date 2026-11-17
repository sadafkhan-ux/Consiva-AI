"""A thin Enterprise wrapper over the existing model client: structured output, a timeout, bounded retries, and an honest record of what answered.

The existing client (app.llm.client) is imported read-only. It asks Claude first with strict JSON output and falls back to the NVIDIA model. This module adds nothing to that behaviour; it
only (1) fixes the call shape Enterprise code uses, (2) validates the answer against a pydantic schema again, (3) retries a bounded number of times with backoff, and (4) turns every way
of failing into one exception, `LLMFailure`, carrying the attempts and the error.

A model, API or parsing failure is an `LLMFailure`. The caller records the chunk as `failed` and the parameters it feeds become NOT ASSESSED. A failure is never a finding about the site.

The model callable is injectable: `async def llm(system: str, user: str, schema: type[BaseModel]) -> LLMResult | BaseModel | dict | str`. A dict or a JSON string is validated against `schema`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.enterprise.analysis import constants as K

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult:
    parsed: BaseModel | dict | str
    provider: str | None = None
    model: str | None = None


@dataclass
class Answer:
    """What a successful call returns to the pipeline."""
    parsed: BaseModel
    provider: str | None
    model: str | None
    attempts: int


class LLMFailure(Exception):
    def __init__(self, message: str, *, attempts: int, provider: str | None = None, model: str | None = None):
        super().__init__(message)
        self.attempts, self.provider, self.model = attempts, provider, model


LLMCallable = Callable[[str, str, type[BaseModel]], Awaitable["LLMResult | BaseModel | dict | str"]]


def default_llm(*, scan_id: str | None = None, agent: str = "enterprise_analysis") -> LLMCallable:
    """The live model, through the existing client (Claude first, NVIDIA as the fallback). Token use is recorded by the existing usage tracking."""

    async def call(system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        from app.llm.client import generate_structured_with_fallback         # imported here: building this module needs no keys or network

        deadline = time.time() + K.LLM_TIMEOUT_S
        from app.monitoring import events as error_events      # the error monitor: a failing provider is recorded (and an administrator emailed once), a working one clears it
        try:
            parsed, meta = await generate_structured_with_fallback(
                system=system, user=user, schema=schema, max_attempts=K.LLM_CLIENT_ATTEMPTS, deadline_epoch=deadline, agent=agent, scan_id=scan_id, stage="analysis")
        except Exception as exc:
            await error_events.record_error(agent="enterprise", module="enterprise.analysis.llm", exc=exc, service="llm", scan_id=scan_id)
            raise
        await error_events.record_success("llm", agent="enterprise")
        if meta.get("fallback_triggered") or not meta.get("primary_attempted"):
            return LLMResult(parsed, meta.get("fallback_provider"), meta.get("fallback_model"))
        return LLMResult(parsed, meta.get("primary_provider"), meta.get("primary_model"))

    return call


def _validate(raw, schema: type[T]) -> T:
    if isinstance(raw, schema):
        return raw
    if isinstance(raw, BaseModel):
        return schema.model_validate(raw.model_dump())
    if isinstance(raw, (str, bytes)):
        return schema.model_validate_json(raw)
    if isinstance(raw, dict):
        return schema.model_validate(raw)
    raise ValueError(f"the model returned {type(raw).__name__}, not JSON")


async def call_structured(
    llm: LLMCallable, system: str, user: str, schema: type[T], *, attempts: int | None = None, timeout_s: float | None = None, backoff_s: tuple[float, ...] | None = None,
) -> Answer:
    """Call the model until it returns an answer that validates against `schema`, at most `attempts` times, with backoff between attempts. Raises LLMFailure when it cannot."""
    attempts = K.LLM_ATTEMPTS if attempts is None else attempts
    timeout_s = K.LLM_TIMEOUT_S if timeout_s is None else timeout_s
    backoff_s = K.LLM_BACKOFF_S if backoff_s is None else backoff_s
    last: Exception | None = None
    provider = model = None
    for n in range(1, attempts + 1):
        try:
            raw = await asyncio.wait_for(llm(system, user, schema), timeout=timeout_s)
            if isinstance(raw, LLMResult):
                provider, model, raw = raw.provider or provider, raw.model or model, raw.parsed
            return Answer(_validate(raw, schema), provider, model, n)
        except asyncio.CancelledError:
            raise
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last = exc
            logger.warning("enterprise analysis: the model's answer did not validate against %s (attempt %d/%d): %s", schema.__name__, n, attempts, str(exc)[:200])
        except Exception as exc:                                  # timeout, transport, provider error, exhausted fallback: all the same to the caller
            last = exc
            logger.warning("enterprise analysis: the model call failed (attempt %d/%d): %s: %s", n, attempts, type(exc).__name__, str(exc)[:200])
        if n < attempts:
            await asyncio.sleep(backoff_s[min(n - 1, len(backoff_s) - 1)] if backoff_s else 0)
    raise LLMFailure(f"{type(last).__name__}: {str(last)[:300]}", attempts=attempts, provider=provider, model=model)
