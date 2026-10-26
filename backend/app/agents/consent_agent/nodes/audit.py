import logging
import uuid
from datetime import UTC, datetime

from app.agents.consent_agent.state import AgentState
from app.db.models import AgentRun
from app.db.repositories import audit_repository
from app.db.session import async_session_factory
from app.services import report_cache_service
from app.observability.stage_tracker import track_stage
from app.services import policy_background, score_deduction_log_service

logger = logging.getLogger(__name__)


async def write_audit_log(state: AgentState) -> dict:
    """Step 13 — terminal node for BOTH the success and failure paths (see graph.py's
    routing after validate_output): a failed run is audited too, not silently dropped."""
    # The policy reading runs beside the graph; the run is not completed until it has finished (services/policy_background.py).
    await policy_background.wait(uuid.UUID(state.agent_run_id))
    async with track_stage(uuid.UUID(state.scan_id), "audit_saved", agent_run_id=uuid.UUID(state.agent_run_id)) as meta:
        async with async_session_factory() as db:
            agent_run = await db.get(AgentRun, uuid.UUID(state.agent_run_id))
            if agent_run is None:
                return {}

            agent_run.completed_at = datetime.now(UTC)
            if state.error:
                agent_run.status = "failed"
                agent_run.error = state.error
                action = "agent_run.failed"
            else:
                agent_run.status = "completed"
                action = "agent_run.completed"

            await audit_repository.record(
                db,
                org_id=uuid.UUID(state.org_id),
                actor_user_id=None,
                action=action,
                entity_type="agent_run",
                entity_id=agent_run.id,
                # provider is real, resolved data from agent_runs.llm_provider (set up
                # front in analysis_service.run_analysis from settings.llm_provider) --
                # never fabricated. No schema change: audit_logs.after is already JSONB.
                after={
                    "created_finding_ids": state.created_finding_ids, "error": state.error,
                    "provider": agent_run.llm_provider,
                },
                agent_run_id=agent_run.id,
                model_name=agent_run.llm_model,
            )
            await db.commit()
            # The scoring formula and the deductions go to the server log and the audit table, never to the client-facing report. A failure here costs
            # that record only: the run's own audit row above is already committed.
            try:
                await score_deduction_log_service.record_scan_deductions(
                    db, scan_id=uuid.UUID(state.scan_id), org_id=uuid.UUID(state.org_id), agent_run_id=agent_run.id)
                await db.commit()
            except Exception:  # best-effort by design
                await db.rollback()
                logger.warning("The score deductions of scan %s could not be recorded", state.scan_id, exc_info=True)
            # A person's review of the findings has just finished, which changes what the report says: print it again ahead of the next download.
            await report_cache_service.queue_prewarm(uuid.UUID(state.scan_id), uuid.UUID(state.org_id))
        meta["action"] = action
        meta["findings_count"] = len(state.created_finding_ids)

    return {}
