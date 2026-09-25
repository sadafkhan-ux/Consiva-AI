import uuid

from app.agents.consent_agent.state import AgentState
from app.db.repositories import finding_repository
from app.db.session import async_session_factory
from app.llm.schemas import ConsentAnalysisResponse, DpdpReference
from app.observability.stage_tracker import track_stage


def _resolve_citations(chunk_ids: list[str], rag_chunks: list[dict]) -> list[dict]:
    """Expands the LLM's raw chunk_id citations into the structured {chunk_id, section,
    source_doc, version} shape, reading section/source_doc/version from the retrieved
    chunk's own (backend-controlled) metadata — never from anything the LLM wrote.
    validate_output.py has already confirmed every id here was actually retrieved."""
    by_id = {c["chunk_id"]: c for c in rag_chunks}
    resolved = []
    for chunk_id in chunk_ids:
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue  # validate_output.py should have caught this already
        resolved.append(DpdpReference(
            chunk_id=chunk_id,
            section=chunk.get("section"),
            source_doc=chunk["document_title"],
            version=chunk.get("document_version"),
        ).model_dump())
    return resolved


def _needs_review(finding) -> bool:
    """Whether a person must read this before it reaches a customer.

    Deliberately NOT left to the model. `requires_human_review` is a field the LLM
    fills in, and different models answer it differently for the same evidence --
    measured directly when the reasoning provider changed: on an identical prompt
    describing analytics firing before consent with no CMP present, one model returned
    True and openai/gpt-oss-120b returned False on every run.

    That difference was established by a control, not assumed: the same prompt was run
    with and without a prompt-injection payload that demanded
    `requires_human_review: false`, and the answer was False in both cases -- so it is
    the model's own default on a clear-cut violation, not something an attacker
    induced. (The injection's other two demands, downgrade risk_level and omit the
    tracking issue, failed in every run.)

    Either way, a high-risk DPDP finding reaching a customer unread should not depend
    on which provider answered. So the model's answer is honoured only to ESCALATE,
    never to waive: high risk always gets a person. This mirrors what
    create_rule_findings already does on the failure path.
    """
    return bool(finding.requires_human_review) or finding.risk_level == "high"


async def create_findings(state: AgentState) -> dict:
    """Steps 10-11 — persists each validated finding + its recommendation. Every
    finding lands as status="pending"; nothing here marks anything approved."""
    response = ConsentAnalysisResponse.model_validate(state.llm_output)
    created_ids: list[str] = []
    escalated = 0

    async with track_stage(
        uuid.UUID(state.scan_id), "findings_generated", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        async with async_session_factory() as db:
            for finding in response.findings:
                required = _needs_review(finding)
                if required and not finding.requires_human_review:
                    # Recorded, not silently corrected: the audit trail should show
                    # that the platform overrode the model here, and how often.
                    escalated += 1
                    finding = finding.model_copy(update={"requires_human_review": True})
                row = await finding_repository.create_finding(
                    db,
                    scan_id=uuid.UUID(state.scan_id),
                    agent_run_id=uuid.UUID(state.agent_run_id),
                    finding=finding,
                    dpdp_reference=_resolve_citations(finding.dpdp_reference, state.rag_chunks),
                )
                created_ids.append(str(row.id))
            await db.commit()
        meta["findings_created"] = len(created_ids)
        meta["risk_levels"] = [f.risk_level for f in response.findings]
        meta["escalated_to_human_review"] = escalated

    return {"created_finding_ids": created_ids}
