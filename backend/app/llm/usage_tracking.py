"""LLM usage and cost tracking.

Every LLM call made for a scan is recorded as one event: who called (agent, stage, scan, agent run), which provider and model answered, the provider's own
token counts (input, output, cache read, cache write, and the reasoning part of the output where the provider reports it), the provider's request ids,
how long the call took, whether it succeeded, and the cost where a price is known. A failed call is recorded too: a refusal or an exhausted retry loop
is still billed by the provider.

Where it goes:
  * the `llm_usage_events` table (migration 0041), one row per call group and provider, idempotent on `call_id` (the same call reported twice is stored
    once); this is what the per-scan usage report reads;
  * the flat file `logs/llm_usage.jsonl`, kept as before for a quick look in a container (its path can be moved with CONSIVA_LLM_USAGE_LOG).

Rules this module keeps:
  * Best-effort. A tracking failure never breaks the LLM call it describes: every error here is caught and logged.
  * Nothing sensitive is stored: no prompt, no response, no document text, no keys -- counts and identifiers only.
  * Nothing is shared between calls. Each event is built from that call's own usage sink, so concurrent scans cannot mix their numbers.
  * A missing or partial usage block records zeros with `reasoning_tokens` NULL and `cost_usd` NULL (unknown), never an invented number.
  * Reasoning tokens are part of the output tokens the provider reports (Anthropic: output_tokens_details.thinking_tokens is inside output_tokens). They are
    kept in their own column for analysis and are NEVER added to the output total or priced a second time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "llm_usage.jsonl"
# Kept as a module constant for anything that imported it; the live path is _log_path(), which honours CONSIVA_LLM_USAGE_LOG.
USAGE_LOG_PATH = _DEFAULT_LOG_PATH

PRICE_SOURCE = "platform.claude.com/docs/en/about-claude/pricing, fetched 2026-10-23"
CURRENCY = "USD"


@dataclass(frozen=True)
class Price:
    """$ per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    # Claude Haiku 5.5 is priced by prompt length: a request whose prompt (input + cache reads + cache writes) is over 100,000 tokens pays the `long` prices.
    long: Price | None = None
    long_over_tokens: int = 100_000


# Anthropic first-party API rates. Only the models this project can be pointed at need a row; a model without one is "not priced" (cost NULL).
ANTHROPIC_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125, 0.20, long=Price(0.50, 2.50, 0.05, 0.625, 1.00)),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10, 2.50, 4.00),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00, 8.00),
}
# Kept for readers of the old (input, output) table.
ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {m: (p.input, p.output) for m, p in ANTHROPIC_PRICES.items()}


def cost_components(provider: str, model: str, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                    cache_write_1h_tokens: int = 0) -> dict | None:
    """USD for one request's usage, split into what was paid for input, cache reads, cache writes and output, plus the total; None when this provider/model has
    no known price (never zeros: unpriced is not free). Anthropic reports input_tokens WITHOUT the cache reads and writes, which are billed at their own rates;
    output_tokens already includes reasoning, so reasoning is never priced a second time."""
    if provider != "claude":
        return None
    price = ANTHROPIC_PRICES.get(model)
    if price is None:
        return None
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    if price.long is not None and prompt > price.long_over_tokens:
        price = price.long
    write_5m = max(0, cache_write_tokens - cache_write_1h_tokens)
    parts = {
        "input": input_tokens * price.input / 1_000_000, "cache_read": cache_read_tokens * price.cache_read / 1_000_000,
        "cache_write": (write_5m * price.cache_write_5m + cache_write_1h_tokens * price.cache_write_1h) / 1_000_000, "output": output_tokens * price.output / 1_000_000,
    }
    return {**parts, "total": sum(parts.values())}


def compute_cost(provider: str, model: str, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                 cache_write_1h_tokens: int = 0) -> float | None:
    """The total of cost_components, or None when there is no known price."""
    parts = cost_components(provider, model, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens, cache_write_1h_tokens=cache_write_1h_tokens)
    return None if parts is None else parts["total"]


def _usd_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    return compute_cost(provider, model, input_tokens=input_tokens, output_tokens=output_tokens)


def summarise_usage(usage_by_attempt: list[dict | None]) -> dict:
    """Adds up the provider's own usage blocks, one per request attempt. Understands the Anthropic shape (input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, output_tokens_details.thinking_tokens) and the OpenAI-compatible shape (prompt_tokens,
    completion_tokens, prompt_tokens_details.cached_tokens, completion_tokens_details.reasoning_tokens). `reasoning_tokens` is None unless at least one
    attempt reported it."""
    inp = out = read = write = write_1h = 0
    reasoning: int | None = None
    request_ids: list[str] = []
    for u in usage_by_attempt:
        u = u or {}
        inp += int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
        out += int(u.get("output_tokens") or u.get("completion_tokens") or 0)
        read += int(u.get("cache_read_input_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        write += int(u.get("cache_creation_input_tokens") or 0)
        write_1h += int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        thinking = (u.get("output_tokens_details") or {}).get("thinking_tokens")
        if thinking is None:
            thinking = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if thinking is not None:
            reasoning = (reasoning or 0) + int(thinking)
        if u.get("request_id"):
            request_ids.append(str(u["request_id"]))
    return {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": read, "cache_write_tokens": write, "cache_write_1h_tokens": write_1h,
            "reasoning_tokens": reasoning, "request_ids": request_ids}


def _log_path() -> Path:
    override = os.environ.get("CONSIVA_LLM_USAGE_LOG")
    return Path(override) if override else USAGE_LOG_PATH


def _append_jsonl(record: dict) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def record_llm_call(*, provider: str, model: str, agent: str, usage_sink: dict, fallback_meta: dict, scan_id: str | None = None) -> dict:
    """Appends one usage record to the flat file and returns it. Best-effort: every exception is caught and logged, never raised."""
    record = {
        "ts": time.time(), "provider": provider, "model": model, "agent": agent, "scan_id": scan_id, "request_count": 0, "input_tokens": 0, "output_tokens": 0,
        "cost_usd": None, "fallback_triggered": bool(fallback_meta.get("fallback_triggered", False)),
    }
    try:
        s = summarise_usage(usage_sink.get("usage_by_attempt", []) or [])
        record["request_count"] = len(usage_sink.get("usage_by_attempt", []) or [])
        record["input_tokens"], record["output_tokens"] = s["input_tokens"], s["output_tokens"]
        record["cache_read_tokens"], record["cache_write_tokens"], record["reasoning_tokens"] = s["cache_read_tokens"], s["cache_write_tokens"], s["reasoning_tokens"]
        record["cost_usd"] = compute_cost(provider, model, input_tokens=s["input_tokens"], output_tokens=s["output_tokens"], cache_read_tokens=s["cache_read_tokens"],
                                          cache_write_tokens=s["cache_write_tokens"], cache_write_1h_tokens=s["cache_write_1h_tokens"])
        _append_jsonl(record)
    except Exception:
        logger.warning("llm usage tracking failed (best-effort; the LLM call itself already succeeded)", exc_info=True)
    return record


_CALL_NAMESPACE = uuid.UUID("5d6a54d6-3c0f-4c1e-9f0b-2f7d7b9f6a11")


def call_id_for(call_group: uuid.UUID, provider: str, role: str) -> uuid.UUID:
    """The same call group and provider always give the same id, so reporting a call twice stores it once."""
    return uuid.uuid5(_CALL_NAMESPACE, f"{call_group}:{provider}:{role}")


def build_event(*, provider: str, model: str, agent: str, usage_sink: dict, fallback_meta: dict, scan_id: str | None, agent_run_id: str | None = None,
                stage: str | None = None, status: str = "success", error_type: str | None = None, duration_ms: int | None = None,
                call_group: uuid.UUID | None = None, role: str = "only", page_urls: list[str] | None = None) -> dict | None:
    """The row for one call group, or None when it cannot be tied to a scan (only scan-bound calls are stored in the table)."""
    try:
        scan_uuid = uuid.UUID(str(scan_id)) if scan_id else None
    except ValueError:
        scan_uuid = None
    if scan_uuid is None:
        return None
    attempts_usage = usage_sink.get("usage_by_attempt", []) or []
    s = summarise_usage(attempts_usage)
    try:
        run_uuid = uuid.UUID(str(agent_run_id)) if agent_run_id else None
    except ValueError:
        run_uuid = None
    cost = compute_cost(provider, model, input_tokens=s["input_tokens"], output_tokens=s["output_tokens"], cache_read_tokens=s["cache_read_tokens"],
                        cache_write_tokens=s["cache_write_tokens"], cache_write_1h_tokens=s["cache_write_1h_tokens"])
    return {
        "call_id": call_id_for(call_group or uuid.uuid4(), provider, role), "scan_id": scan_uuid, "agent_run_id": run_uuid, "agent": agent, "stage": stage,
        "provider": provider, "model": model, "request_ids": s["request_ids"], "attempts": len(attempts_usage), "input_tokens": s["input_tokens"],
        "output_tokens": s["output_tokens"], "cache_read_tokens": s["cache_read_tokens"], "cache_write_tokens": s["cache_write_tokens"],
        "reasoning_tokens": s["reasoning_tokens"], "status": status, "error_type": (error_type or None) and error_type[:120],
        "fallback_triggered": bool(fallback_meta.get("fallback_triggered", False)), "duration_ms": duration_ms,
        "cost_usd": None if cost is None else round(cost, 6), "currency": CURRENCY, "price_source": PRICE_SOURCE if cost is not None else None,
        "page_urls": [str(u)[:2000] for u in (page_urls or [])][:50],
    }


async def record_llm_event(**kw) -> dict | None:
    """Records one call group: to the flat file and to llm_usage_events. Never raises. Returns the stored row's fields (or None when there was no scan to
    tie it to). Set CONSIVA_DISABLE_USAGE_DB=1 to skip the database write (the unit tests do)."""
    try:
        record_llm_call(provider=kw["provider"], model=kw["model"], agent=kw["agent"], usage_sink=kw["usage_sink"], fallback_meta=kw["fallback_meta"], scan_id=kw.get("scan_id"))
        event = build_event(**kw)
        if event is None or os.environ.get("CONSIVA_DISABLE_USAGE_DB") == "1":
            return event
        from app.db.repositories import usage_repository
        from app.db.session import async_session_factory

        async def write() -> None:
            async with async_session_factory() as db:
                await usage_repository.insert_event(db, event)
                await db.commit()

        await asyncio.wait_for(write(), timeout=10)
        return event
    except Exception:
        logger.warning("llm usage event could not be stored (best-effort; the LLM call itself is unaffected)", exc_info=True)
        return None
