import logging
import uuid

from app.agents.consent_agent.state import AgentState
from app.config import get_settings
from app.db.session import async_session_factory
from app.llm.client import NvidiaLLMClient
from app.observability.stage_tracker import track_stage
from app.rag.retriever import has_text, retrieve_many, text_only_query

logger = logging.getLogger(__name__)

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
#
# That measurement was for ONE combined query covering every rule finding in the scan
# -- which is also what produced the "every finding cites the same DPDP reference" bug:
# a single blended query ("...relevant to: no privacy policy found; analytics fired
# before consent; no cookie policy found") retrieves the top_k chunks closest to that
# AVERAGE topic, not the chunks relevant to each distinct violation, so a finding about
# consent notice and a finding about purpose limitation were handed the exact same
# small pool and had nothing else to cite. Retrieving per rule finding instead (each
# finding's own `summary` as its own query) gives each violation type a chance at its
# own provision; results are merged and de-duplicated by chunk_id so an overlapping
# topic across findings doesn't inflate the prompt.
_TOP_K_PER_QUERY = 3
_MAX_TOTAL_CHUNKS = 8


def _queries(rule_findings: list[dict]) -> list[str]:
    if not rule_findings:
        return ["General DPDP Act consent, cookie, and website tracking compliance requirements"]
    # De-duplicated so two rule findings with the literal same summary (e.g. R-001
    # firing identically on two scans merged into one run) don't retrieve twice.
    return sorted({f["summary"] for f in rule_findings})


async def retrieve_rag(state: AgentState) -> dict:
    """Step 7 — pgvector similarity search over approved DPDP knowledge, run once per
    distinct rule finding so each violation type is scoped to its own retrieval rather
    than one query blended across all of them."""
    raw_queries = _queries(state.rule_findings)
    # The embedding model rejects an image as a query (rag.retriever.text_only_query). Each query is sent
    # as text; a query that was only an image is dropped, and if that leaves none the general query runs,
    # so retrieval never silently returns nothing because of an image.
    queries = [q for q in (text_only_query(q) for q in raw_queries) if has_text(q)]
    if not queries:
        queries = _queries([])
    sanitised = sum(1 for q in raw_queries if text_only_query(q) != q)
    llm_client = NvidiaLLMClient()

    try:
        async with track_stage(uuid.UUID(state.scan_id), "rag_retrieval", agent_run_id=uuid.UUID(state.agent_run_id)) as meta:
            meta["queries"] = queries
            if sanitised:
                meta["queries_text_only"] = sanitised  # how many had an inline image replaced by a text marker
            max_distance = get_settings().rag_max_distance
            merged: dict[str, object] = {}
            stats: dict = {}
            async with async_session_factory() as db:
                # One embedding request for all the queries, each still searched on its own.
                try:
                    per_query = await retrieve_many(
                        queries, db=db, llm_client=llm_client, top_k=_TOP_K_PER_QUERY,
                        max_distance=max_distance, stats=stats,
                    )
                finally:
                    meta["timings"] = {k: stats[k] for k in ("embed_calls", "embed_ms", "search_ms", "cache_hits") if k in stats}
            if stats.get("knowledge_base_empty"):
                meta["knowledge_base_empty"] = True   # visible on the stage: no provisions exist to cite
            for chunks_for_query in per_query:
                for chunk in chunks_for_query:
                    existing = merged.get(chunk.chunk_id)
                    if existing is None or chunk.distance < existing.distance:
                        merged[chunk.chunk_id] = chunk
            chunks = sorted(merged.values(), key=lambda c: c.distance)[:_MAX_TOTAL_CHUNKS]

            meta["chunks_retrieved"] = len(chunks)
            meta["max_distance"] = max_distance
            meta["results"] = [
                {
                    "chunk_id": c.chunk_id, "source_document": c.document_title,
                    "section": c.section, "distance": round(c.distance, 4),
                }
                for c in chunks
            ]

    except Exception as exc:
        # The stage row already says it failed, with the error (track_stage records it before re-raising).
        # Letting it propagate ended the whole analysis with no findings at all, although the rules had
        # already matched. Retrieval only supplies citations, so the run continues without the model and
        # writes the rule-derived findings, each flagged for human review and citing nothing.
        logger.exception("RAG retrieval failed for scan %s; continuing with rule-derived findings", state.scan_id)
        return {
            "rag_chunks": [], "rag_failed": True,
            "error": f"retrieval of regulatory provisions failed ({type(exc).__name__}: {str(exc)[:240]})",
        }

    return {"rag_chunks": [c.as_state_dict() for c in chunks]}
