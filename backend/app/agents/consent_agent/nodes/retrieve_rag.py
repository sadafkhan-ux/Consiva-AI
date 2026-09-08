import uuid

from app.agents.consent_agent.state import AgentState
from app.config import get_settings
from app.db.session import async_session_factory
from app.llm.client import NvidiaLLMClient
from app.observability.stage_tracker import track_stage
from app.rag.retriever import retrieve

# Was reduced 6->4 in an earlier optimization pass to shrink the LLM prompt, but that
# was later measured to yield no reliable wall-clock benefit (NVIDIA endpoint variance
# dominates; see the performance audit). With the knowledge base since doubled from 1
# to 6 documents (105->224 chunks), top_k=4 was directly observed to miss a genuinely
# relevant, correctly-embedded chunk that ranked #2 at top_k=15 for a real query --
# not a source gap, a recall issue. Restored to 6 on that evidence.
_TOP_K = 6


def _build_query(rule_findings: list[dict]) -> str:
    if not rule_findings:
        return "General DPDP Act consent, cookie, and website tracking compliance requirements"
    return "DPDP Act compliance requirements relevant to: " + "; ".join(f["summary"] for f in rule_findings)


async def retrieve_rag(state: AgentState) -> dict:
    """Step 7 — pgvector similarity search over approved DPDP knowledge, scoped by
    what the rules already flagged so retrieval stays targeted."""
    query = _build_query(state.rule_findings)
    llm_client = NvidiaLLMClient()

    async with track_stage(uuid.UUID(state.scan_id), "rag_retrieval", agent_run_id=uuid.UUID(state.agent_run_id)) as meta:
        meta["query"] = query
        max_distance = get_settings().rag_max_distance
        async with async_session_factory() as db:
            chunks = await retrieve(query, db=db, llm_client=llm_client, top_k=_TOP_K, max_distance=max_distance)
        meta["chunks_retrieved"] = len(chunks)
        # Anything short of _TOP_K means the threshold dropped weak matches -- recorded
        # so a sparse-context run is diagnosable from stage metadata alone.
        meta["dropped_by_distance_threshold"] = _TOP_K - len(chunks)
        meta["max_distance"] = max_distance
        meta["results"] = [
            {
                "chunk_id": c.chunk_id, "source_document": c.document_title,
                "section": c.section, "distance": round(c.distance, 4),
            }
            for c in chunks
        ]

    return {"rag_chunks": [c.as_state_dict() for c in chunks]}
