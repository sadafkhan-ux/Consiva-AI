"""Minimal, provider-agnostic LLM usage/cost tracking.

Before this module existed, only Agent 1 (Consent) captured per-call token usage
(via the `usage_sink` passed into generate_structured_with_fallback, persisted into
agent_run_stages.metadata) -- ROPA and RegWatch called the same shared client but
never requested a usage_sink, so their token usage was silently discarded at the
call site, not even requested. There was also no cross-agent place to answer "how
much did this scan cost", only a per-scan JSONB blob shaped for Agent 1 alone.

This is deliberately a flat, append-only JSONL log rather than a new database table:
the smallest thing that makes per-agent/per-scan cost answerable without a schema
migration, a new model, or touching any existing dashboard. It is wired into
generate_structured_with_fallback() in client.py -- the one place every agent's LLM
call already passes through -- so no other call site has to change to get basic
recording; passing `agent`/`scan_id` there is an optional, additive improvement for
attribution, not a requirement.

Never logs prompts, responses, or any document/document content -- only token counts
and identifiers.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

USAGE_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "llm_usage.jsonl"

# $ per million tokens, Anthropic first-party API rates (checked 2026-10-14). Only
# the models this project is actually configured to use need a row -- add one before
# pointing ANTHROPIC_MODEL at something new, or cost_usd below comes back None
# (clearly marked as "not priced", never silently wrong).
ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def _usd_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """None (not 0.0) means "we don't have a price for this provider/model" --
    callers must treat that as unpriced, not as a free call."""
    if provider != "claude":
        return None
    pricing = ANTHROPIC_PRICING_PER_MTOK.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate


def record_llm_call(
    *,
    provider: str,
    model: str,
    agent: str,
    usage_sink: dict,
    fallback_meta: dict,
    scan_id: str | None = None,
) -> dict:
    """Append one usage record for a completed LLM call and return it.

    Best-effort by design: a tracking failure must never break the LLM call it is
    describing, so every exception here is caught and logged, never raised.
    """
    record = {
        "ts": time.time(),
        "provider": provider,
        "model": model,
        "agent": agent,
        "scan_id": scan_id,
        "request_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "fallback_triggered": bool(fallback_meta.get("fallback_triggered", False)),
    }
    try:
        usage_by_attempt = usage_sink.get("usage_by_attempt", []) or []
        input_tokens = sum((u or {}).get("input_tokens") or (u or {}).get("prompt_tokens") or 0 for u in usage_by_attempt)
        output_tokens = sum((u or {}).get("output_tokens") or (u or {}).get("completion_tokens") or 0 for u in usage_by_attempt)
        record["request_count"] = len(usage_by_attempt)
        record["input_tokens"] = input_tokens
        record["output_tokens"] = output_tokens
        record["cost_usd"] = _usd_cost(provider, model, input_tokens, output_tokens)
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        logger.warning("llm usage tracking failed (best-effort; the LLM call itself already succeeded)", exc_info=True)
    return record
