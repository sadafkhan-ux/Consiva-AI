import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import openai
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import LLMOutputValidationError
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.llm.client import NvidiaLLMClient

logger = logging.getLogger(__name__)


# The configured embedding model is a vision-language one (nvidia/llama-nemotron-embed-vl-1b-v2). It accepts
# an image as a DOCUMENT but rejects one as a QUERY: HTTP 400 "This model does not support image input as
# query". It treats a query that is an inline image (a data: URI) as an image. A retrieval query is a sentence
# about a finding, so an image in one is noise; it is replaced by a short text marker before embedding. The
# evidence the query came from is untouched -- only the string sent to the embedding model changes.
_INLINE_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+(?:;[A-Za-z0-9=.+-]+)*;base64,[A-Za-z0-9+/=_-]*", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKERS = re.compile(r"\[(?:inline image|image|link)\]")


def text_only_query(query: str, *, strip_urls: bool = False) -> str:
    """`query` with any inline image replaced by a text marker. `strip_urls` also drops URLs, the
    second, stricter attempt used only if the embedding API still reports an image."""
    cleaned = _INLINE_IMAGE.sub("[inline image]", query)
    cleaned = _IMG_TAG.sub("[image]", cleaned)
    if strip_urls:
        cleaned = _URL.sub("[link]", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def has_text(query: str) -> bool:
    """Whether anything is left to retrieve with once the markers are ignored."""
    return bool(re.search(r"[A-Za-z]{3,}", _MARKERS.sub(" ", query)))


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    document_version: str | None
    content: str
    distance: float
    metadata: dict

    @property
    def section(self) -> str | None:
        return self.metadata.get("section")

    def as_prompt_dict(self) -> dict:
        """Lean view for the LLM prompt — just enough to cite by chunk_id. Section/
        version live on the full dataclass for create_findings.py to build the
        structured citation objects; the LLM never needs to transcribe them itself."""
        return {"chunk_id": self.chunk_id, "document_title": self.document_title, "content": self.content}

    def as_state_dict(self) -> dict:
        """Full view persisted into AgentState.rag_chunks — everything downstream
        consumers (prompting, citation-building) need, in one place."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "document_version": self.document_version,
            "content": self.content,
            "section": self.section,
        }


async def retrieve(
    query: str, *, db: AsyncSession, llm_client: NvidiaLLMClient, top_k: int = 5,
    max_distance: float | None = None,
) -> list[RetrievedChunk]:
    """Cosine-similarity search over approved knowledge_chunks only (docs/architecture
    §I) — unapproved/draft documents never reach the LLM.

    `max_distance`, when set, drops chunks whose cosine distance exceeds it instead of
    padding the result out to top_k with barely-related material — a query about a
    topic genuinely absent from the knowledge base should return LESS context, not
    fake-relevant context the LLM is then invited to cite. Filtered in Python after
    the top_k fetch (k is small) so the SQL stays one simple ordered query."""
    text_query = text_only_query(query)
    if not has_text(text_query):
        # Nothing but an image: there is no sentence to retrieve provisions for. Returning no chunks is
        # the honest answer; the caller's own evidence and rule findings are unaffected.
        logger.warning("Retrieval query had no text once inline images were removed; returning no chunks")
        return []
    if text_query != query:
        logger.info("Retrieval query contained an inline image; embedding its text only")
    query_embedding = await _embed_one(query, text_query, llm_client)
    return await _search(query_embedding, db=db, top_k=top_k, max_distance=max_distance)


async def _embed_one(query: str, text_query: str, llm_client: NvidiaLLMClient) -> list[float]:
    """Embed one already-sanitised query, with the single stricter retry if the model still reads it as an
    image. Cached by text: an embedding is a pure function of (model, dimensions, text)."""
    cached = _cache_get(text_query)
    if cached is not None:
        return cached
    try:
        [embedding] = await llm_client.embed([text_query], input_type="query")
    except openai.BadRequestError as exc:
        if "image input" not in str(exc).lower():
            raise
        # Still read as an image after the inline images were removed. One stricter attempt, then a clear
        # error -- never a silent empty result that would look like "no relevant provisions exist".
        stricter = text_only_query(query, strip_urls=True)
        if stricter == text_query or not has_text(stricter):
            raise LLMOutputValidationError(
                "The embedding model rejected the retrieval query as image input and no text-only form of it "
                f"was available: {exc}"
            ) from exc
        [embedding] = await llm_client.embed([stricter], input_type="query")
    _cache_put(text_query, embedding)
    return embedding


async def _search(
    query_embedding: list[float], *, db: AsyncSession, top_k: int, max_distance: float | None,
) -> list[RetrievedChunk]:
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.title, KnowledgeDocument.version, distance)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.is_approved.is_(True))
        .order_by(distance)
        .limit(top_k)
    )
    result = await db.execute(stmt)

    chunks = [
        RetrievedChunk(
            chunk_id=str(chunk.id),
            document_id=str(chunk.document_id),
            document_title=title,
            document_version=version,
            content=chunk.content,
            distance=float(dist),
            metadata=chunk.chunk_metadata,
        )
        for chunk, title, version, dist in result.all()
    ]
    if max_distance is not None:
        chunks = [c for c in chunks if c.distance <= max_distance]
    return chunks


# ── query-embedding cache and batched retrieval ──────────────────────────────────
# An embedding depends only on (model, dimensions, text), so a repeated query -- the same rule summary on a
# second scan -- never needs a second network call. Bounded, in-process, least-recently-used.
_EMBED_CACHE: "OrderedDict[tuple[str, int, str], list[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 256


def _cache_key(text: str) -> tuple[str, int, str]:
    settings = get_settings()
    return (settings.nvidia_embed_model, settings.nvidia_embed_dimensions, text)


def _cache_get(text: str) -> list[float] | None:
    key = _cache_key(text)
    value = _EMBED_CACHE.get(key)
    if value is not None:
        _EMBED_CACHE.move_to_end(key)
    return value


def _cache_put(text: str, embedding: list[float]) -> None:
    _EMBED_CACHE[_cache_key(text)] = embedding
    _EMBED_CACHE.move_to_end(_cache_key(text))
    while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)


def clear_query_embedding_cache() -> None:
    _EMBED_CACHE.clear()


async def _knowledge_base_has_chunks(db: AsyncSession) -> bool:
    result = await db.execute(
        select(KnowledgeChunk.id)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.is_approved.is_(True))
        .limit(1)
    )
    return result.first() is not None


async def retrieve_many(
    queries: list[str], *, db: AsyncSession, llm_client: NvidiaLLMClient, top_k: int = 5,
    max_distance: float | None = None, stats: dict | None = None,
) -> list[list[RetrievedChunk]]:
    """`retrieve()` for several queries, with the network cost paid once: results come back in the same order,
    each query still searched on its own so a violation type keeps its own pool of provisions.

      - an approved knowledge base with no chunks is detected up front (one cheap query) and skips embedding
        altogether -- there is nothing the embeddings could be compared with;
      - queries whose embedding is cached cost nothing;
      - the rest are embedded in ONE request, not one request each. If that request is refused as an image,
        each query falls back to the single-query path (with its stricter retry), so one bad query cannot
        take the others down.

    `stats`, if given, is filled with what happened and how long each part took, for the stage record.
    """
    stats = stats if stats is not None else {}
    stats.update(embed_calls=0, embed_ms=0, search_ms=0, cache_hits=0, knowledge_base_empty=False)

    if not await _knowledge_base_has_chunks(db):
        logger.warning(
            "RAG knowledge base has no approved chunks; skipping %d retrieval quer%s. Findings will cite nothing.",
            len(queries), "y" if len(queries) == 1 else "ies",
        )
        stats["knowledge_base_empty"] = True
        return [[] for _ in queries]

    texts = [text_only_query(q) for q in queries]
    embeddings: dict[str, list[float]] = {}
    pending: list[str] = []
    for text in texts:
        if not has_text(text) or text in embeddings or text in pending:
            continue
        cached = _cache_get(text)
        if cached is not None:
            embeddings[text] = cached
            stats["cache_hits"] += 1
        else:
            pending.append(text)

    if pending:
        started = time.perf_counter()
        try:
            stats["embed_calls"] += 1
            vectors = await llm_client.embed(pending, input_type="query")
            if len(vectors) != len(pending):
                raise LLMOutputValidationError(
                    f"Embedding service returned {len(vectors)} vectors for {len(pending)} queries."
                )
            for text, vector in zip(pending, vectors, strict=True):
                embeddings[text] = vector
                _cache_put(text, vector)
        except openai.BadRequestError as exc:
            if "image input" not in str(exc).lower():
                raise
            for text, original in ((t, q) for q in queries for t in [text_only_query(q)] if t in pending):
                if text in embeddings:
                    continue
                stats["embed_calls"] += 1
                embeddings[text] = await _embed_one(original, text, llm_client)
        stats["embed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    results: list[list[RetrievedChunk]] = []
    for text in texts:
        if text not in embeddings:
            results.append([])      # nothing but an image: no sentence to retrieve provisions for
            continue
        results.append(await _search(embeddings[text], db=db, top_k=top_k, max_distance=max_distance))
    stats["search_ms"] = int((time.perf_counter() - started) * 1000)
    return results
