import uuid
from datetime import UTC, datetime

from app.agents.consent_agent.state import AgentState
from app.db.models import AgentRun
from app.db.repositories import audit_repository
from app.db.session import async_session_factory
from app.observability.stage_tracker import track_stage


async def write_audit_log(state: AgentState) -> dict:
    """Step 13 — terminal node for BOTH the success and failure paths (see graph.py's
    routing after validate_output): a failed run is audited too, not silently dropped."""
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
        meta["action"] = action
        meta["findings_count"] = len(state.created_finding_ids)

    return {}
