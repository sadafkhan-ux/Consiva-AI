"""Hybrid retrieval over one website's current policy chunks: keyword search and semantic search, fused.

Always scoped by organisation and website, and only over chunks of a document's CURRENT version (`is_active`): a customer's, a website's or an old version's text can never be returned for another's
question. If the query cannot be embedded, the answer is the keyword result alone and `mode` says so; it is never silently empty. Every call is logged (the retrieval count on the dashboard comes
from that log). Each result carries its source: document, version, address, heading and chunk position, so an assessment can cite exactly what it read.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise.policykb import sync as kb

logger = logging.getLogger(__name__)

_RRF_K = 60

_KEYWORD = text("""
    select c.id, c.doc_id, c.version_id, c.doc_type, c.source_url, c.chunk_index, c.section_heading, c.content, v.version_no, v.extracted_at, d.stale,
           ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) as score
    from ent_policy_chunks c join ent_policy_versions v on v.id = c.version_id join ent_policy_docs d on d.id = c.doc_id
    where c.org_id = :org and c.domain = :dom and c.is_active and (cast(:types as text[]) is null or c.doc_type = any(cast(:types as text[])))
      and c.tsv @@ websearch_to_tsquery('english', :q)
    order by score desc limit :k""")

_SEMANTIC = text("""
    select c.id, c.doc_id, c.version_id, c.doc_type, c.source_url, c.chunk_index, c.section_heading, c.content, v.version_no, v.extracted_at, d.stale,
           1 - (c.embedding <=> cast(:e as vector)) as score
    from ent_policy_chunks c join ent_policy_versions v on v.id = c.version_id join ent_policy_docs d on d.id = c.doc_id
    where c.org_id = :org and c.domain = :dom and c.is_active and c.embedding is not null and (cast(:types as text[]) is null or c.doc_type = any(cast(:types as text[])))
    order by c.embedding <=> cast(:e as vector) limit :k""")


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    version_id: str
    doc_type: str
    source_url: str
    chunk_index: int
    heading: str | None
    content: str
    version_no: int
    extracted_at: str
    stale: bool                       # the document's latest version could not be confirmed: this is the last verified one
    score: float
    matched_by: str                   # "keyword", "semantic" or "both"


def _hit(r, score: float, by: str) -> Hit:
    return Hit(str(r["id"]), str(r["doc_id"]), str(r["version_id"]), r["doc_type"], r["source_url"], r["chunk_index"], r["section_heading"], r["content"], r["version_no"],
               r["extracted_at"].isoformat() if r["extracted_at"] else "", bool(r["stale"]), score, by)


async def retrieve(org_id: uuid.UUID, domain: str, query: str, *, doc_types: list[str] | None = None, top_k: int = 6, parameter_id: str | None = None,
                   query_embedder: kb.Embedder | None = None, log: bool = True) -> dict:
    """{"mode": "hybrid" | "keyword_fallback" | "keyword_only", "hits": [Hit...]}. Fusion is reciprocal-rank: a chunk found by both searches ranks above one found by either alone."""
    set_org_scope(org_id)
    query = (query or "").strip()
    if not query:
        return {"mode": "keyword_only", "hits": []}
    types = doc_types or None
    pool = max(top_k * 3, 12)
    async with async_session_factory() as db:
        kw = (await db.execute(_KEYWORD, {"org": org_id, "dom": domain, "types": types, "q": query, "k": pool})).mappings().all()
        mode, sem = "hybrid", []
        try:
            [vector] = await kb.embed_all([query], query_embedder or kb.default_query_embedder)
            sem = (await db.execute(_SEMANTIC, {"org": org_id, "dom": domain, "types": types, "e": kb._vec(vector), "k": pool})).mappings().all()
        except Exception as exc:                                  # the model could not embed the question: keyword results only, and the caller is told
            logger.warning("policy retrieval: the query could not be embedded (%s); using keyword search only", type(exc).__name__)
            mode = "keyword_fallback"
            from app.monitoring import events as error_events
            await error_events.record_error(agent="enterprise", module="enterprise.policykb.retrieve", exc=exc, service="embedding_api", message="The question could not be embedded; search fell back to keywords only.")
        scores: dict[str, float] = {}
        rows: dict[str, tuple] = {}
        found: dict[str, set[str]] = {}
        for label, result in (("keyword", kw), ("semantic", sem)):
            for rank, r in enumerate(result):
                key = str(r["id"])
                scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
                rows.setdefault(key, r)
                found.setdefault(key, set()).add(label)
        ordered = sorted(scores, key=lambda k: scores[k], reverse=True)[:top_k]
        hits = [_hit(rows[k], round(scores[k], 6), "both" if len(found[k]) == 2 else next(iter(found[k]))) for k in ordered]
        if log:
            await db.execute(text("insert into ent_policy_retrievals (org_id, domain, parameter_id, mode, results) values (:o, :d, :p, :m, :n)"),
                             {"o": org_id, "d": domain, "p": parameter_id, "m": mode, "n": len(hits)})
            await db.commit()
    return {"mode": mode, "hits": [asdict(h) for h in hits]}
