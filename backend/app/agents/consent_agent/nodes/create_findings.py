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


async def create_findings(state: AgentState) -> dict:
    """Steps 10-11 — persists each validated finding + its recommendation. Every
    finding lands as status="pending"; nothing here marks anything approved."""
    response = ConsentAnalysisResponse.model_validate(state.llm_output)
    created_ids: list[str] = []

    async with track_stage(
        uuid.UUID(state.scan_id), "findings_generated", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        async with async_session_factory() as db:
            for finding in response.findings:
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

    return {"created_finding_ids": created_ids}
