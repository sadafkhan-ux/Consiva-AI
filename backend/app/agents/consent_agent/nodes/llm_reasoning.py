import logging
import time
import uuid

from app.agents.consent_agent.state import AgentState
from app.config import get_settings
from app.db.models import AgentRun
from app.db.session import async_session_factory
from app.llm.client import generate_structured_with_fallback
from app.llm.prompts import RETRY_SUFFIX, SYSTEM_PROMPT, build_analysis_prompt
from app.llm.schemas import ConsentAnalysisResponse
from app.observability.stage_tracker import track_stage

logger = logging.getLogger(__name__)


class _AnalysisUnavailable(RuntimeError):
    """Every configured provider failed. Raised inside the stage tracker so the stage
    is recorded as failed, then caught immediately outside it and turned into a state
    the graph can route on -- see the wrapper below."""


async def _llm_reasoning_inner(state: AgentState) -> dict:
    """Step 8 — the only node that calls the LLM. Provider selection (self-hosted
    primary -> NVIDIA/Groq fallback) is entirely owned by
    generate_structured_with_fallback() -- this node has no provider-specific logic
    of its own, only the shared BaseLLMClient.generate_structured() contract via that
    one call. On a retry loop from validate_output, `state.error` carries the
    specific correction to feed back."""
    user_prompt = build_analysis_prompt(
        scan_summary=state.scan_evidence, rule_findings=state.rule_findings, rag_chunks=state.rag_chunks
    )
    if state.error:
        user_prompt += RETRY_SUFFIX.format(error=state.error)

    settings = get_settings()
    # One deadline for the whole analysis's LLM effort, set on the FIRST invocation
    # only -- a graph-level validation retry re-enters this node with the same budget,
    # not a fresh one (see AgentState.llm_deadline_epoch). Shared across primary AND
    # fallback attempts -- a fallback call only gets whatever budget the primary
    # didn't use, never a fresh deadline on top (no timeout multiplication).
    deadline_epoch = state.llm_deadline_epoch or (time.time() + settings.llm_deadline_seconds)

    async with track_stage(uuid.UUID(state.scan_id), "llm_analysis", agent_run_id=uuid.UUID(state.agent_run_id)) as meta:
        meta["prompt_chars"] = len(user_prompt)
        meta["is_retry"] = state.error is not None
        meta["deadline_seconds_remaining"] = round(deadline_epoch - time.time(), 1)
        usage_sink: dict = {}
        try:
            response, fallback_meta = await generate_structured_with_fallback(
                system=SYSTEM_PROMPT, user=user_prompt, schema=ConsentAnalysisResponse,
                settings=settings, usage_sink=usage_sink, deadline_epoch=deadline_epoch,
            )
        except Exception as exc:
            # Degrade, do not explode.
            #
            # This used to propagate, which killed the whole graph run at this node --
            # so `validate_output` never ran, its `failed` route was never taken, and
            # `create_rule_findings` never got the chance to surface the rule matches
            # that were already in hand. Observed on a real hubspot.com scan: three
            # rules matched (including tracking that continued after the visitor
            # pressed Reject), both providers failed, and the customer received
            # exactly zero findings.
            #
            # Returning a failed status instead lets the graph route to the fallback,
            # which persists those rule matches with the narrative marked missing and
            # every one flagged for human review. The analysis still counts as failed
            # -- it is recorded as such on the stage and on the finding text -- but a
            # provider outage now costs the explanation rather than the entire output.
            meta["provider_failure"] = f"{type(exc).__name__}: {exc}"[:500]
            meta["degraded_to_rule_findings"] = True
            logger.warning(
                "LLM analysis failed for scan %s (%s); falling back to rule-derived "
                "findings", state.scan_id, type(exc).__name__,
            )
            raise _AnalysisUnavailable(str(exc)) from exc
        # Real, never-fabricated provider/model + fallback data for section 11's
        # audit/observability ask -- merged straight from generate_structured_with_fallback.
        meta.update(fallback_meta)
        actual_provider = fallback_meta["fallback_provider"] if fallback_meta["fallback_triggered"] else fallback_meta["primary_provider"]
        actual_model = fallback_meta["fallback_model"] if fallback_meta["fallback_triggered"] else fallback_meta["primary_model"]
        meta["provider"] = actual_provider
        meta["model"] = actual_model
        meta["findings_drafted"] = len(response.findings)
        meta["llm_usage"] = usage_sink

        if fallback_meta["fallback_triggered"]:
            # agent_runs.llm_provider/llm_model were stamped up front (before this
            # node ran) with the INTENDED primary -- correct them here to whichever
            # provider actually answered, so write_audit_log's final audit entry
            # (which reads straight from this row) never reports a provider that
            # didn't really generate the result.
            async with async_session_factory() as db:
                agent_run = await db.get(AgentRun, uuid.UUID(state.agent_run_id))
                if agent_run is not None:
                    agent_run.llm_provider = actual_provider
                    agent_run.llm_model = actual_model
                    await db.commit()

    return {"llm_output": response.model_dump(), "error": None, "llm_deadline_epoch": deadline_epoch}


async def llm_reasoning(state: AgentState) -> dict:
    """Public entry point. Converts a total provider failure into a routable state.

    The inner function raises inside `track_stage`, so `llm_analysis` is still
    recorded as failed with its real error -- the audit trail does not pretend the
    call succeeded. Catching it out here is what stops that failure from ending the
    run: `validate_output` sees `validation_status="failed"` and routes to
    `create_rule_findings`.
    """
    try:
        return await _llm_reasoning_inner(state)
    except _AnalysisUnavailable as exc:
        return {
            "llm_output": None,
            "error": str(exc),
            "validation_status": "failed",
            "llm_deadline_epoch": state.llm_deadline_epoch,
        }
