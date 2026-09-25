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
# Measured on the live corpus (279 chunks across the DPDP Act, DPDP Rules, IT Act and
# the gazette updates): chunks average 314 tokens, so top_k=6 spent ~2,250 tokens --
# 10% of the whole prompt -- on retrieval alone.
#
# Findings cite chunk_ids and a citation can only ground in a chunk that was actually
# retrieved, so this number is a ceiling on how many distinct provisions the model can
# ever reference -- which makes it the one RAG setting that can cost accuracy outright.
# So it was measured rather than picked, live against the real corpus and a real scan
# (projectflow.gignaati.com), self-hosted model, identical prompt otherwise:
#
#   top_k=4   in=3,282  out=585  3 findings, 3 with citations, 0 ungrounded
#   top_k=3   in=2,904  out=568  3 findings, 3 with citations, 0 ungrounded
#   top_k=2   in=2,517  out=404  2 findings  <- a finding is lost
#
# 3 is therefore the floor: it holds finding count and citation coverage exactly while
# returning ~380 tokens, and those tokens are what let the response finish on a server
# with n_ctx=4096 (a truncated response at top_k=4 is what started this measurement).
# 2 is past the edge. Re-measure before changing it again.
_TOP_K = 3


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
