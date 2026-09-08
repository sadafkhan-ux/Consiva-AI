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


async def llm_reasoning(state: AgentState) -> dict:
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
        response, fallback_meta = await generate_structured_with_fallback(
            system=SYSTEM_PROMPT, user=user_prompt, schema=ConsentAnalysisResponse,
            settings=settings, usage_sink=usage_sink, deadline_epoch=deadline_epoch,
        )
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
