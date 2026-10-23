import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import openai
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import LLMOutputValidationError
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.llm.client import NvidiaLLMClient
from app.rag.text_quality import is_readable_text
from app.rag.topic_embeddings import stored_vector, topic_of

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


# Rows fetched beyond top_k so that dropping unreadable chunks (see rag/text_quality.py) does not leave a query short. About a
# fifth of the stored knowledge base is unreadable text, so this is generous rather than tight.
_UNREADABLE_HEADROOM = 6


async def _search(
    query_embedding: list[float], *, db: AsyncSession, top_k: int, max_distance: float | None,
) -> list[RetrievedChunk]:
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.title, KnowledgeDocument.version, distance)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.is_approved.is_(True))
        .order_by(distance)
        .limit(top_k + _UNREADABLE_HEADROOM)
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
    # Unreadable (legacy-encoded Hindi) chunks are dropped here and the next-best chunks take their place, so a query
    # still returns top_k useful chunks. The extra rows fetched above exist only to make that possible.
    chunks = [c for c in chunks if is_readable_text(c.content)][:top_k]
    if max_distance is not None:
        chunks = [c for c in chunks if c.distance <= max_distance]
    return chunks


# ── keyword fallback ─────────────────────────────────────────────────────────────
# When the embedding service cannot be reached, the provisions are still found: by the words of the query, using the database's own
# full-text search over the same approved chunks. It ranks less finely than the embeddings do, so the stage records that it was used
# (`retrieval_mode`), and the citation checks downstream apply to what it returns exactly as they do to anything else.
_EMBEDDING_UNAVAILABLE = (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)
_WORDS = re.compile(r"[A-Za-z]{4,}")
# Words that are in every query or in every provision, so they say nothing about which provision is meant.
_STOP_WORDS = frozenset({
    "that", "this", "with", "from", "were", "have", "been", "which", "there", "their", "they", "them", "than", "then", "when", "where",
    "while", "would", "could", "should", "under", "over", "into", "onto", "upon", "also", "such", "some", "other", "about", "after",
    "before", "observed", "found", "scan", "scanned", "page", "pages", "site", "website", "section", "sections", "sub", "clause",
    "shall", "other", "being", "made", "make", "only", "each", "every", "does", "done", "none",
})
_MAX_QUERY_TERMS = 12


def keyword_terms(query: str) -> list[str]:
    """The distinctive words of a query, in order, without repeats: the terms the text search is run on.

    A retrieval query ends with the provision the rule is about, in brackets ("... (consent under section 6, processing personal data
    before consent is given)", see retrieve_rag._query_text). Those words are the ones that decide WHICH provision is meant, whereas the
    rule's own summary talks about cookies and scripts, words the Act does not use. So the bracketed words come first and the term limit
    cuts the summary, not the provision."""
    cleaned = _MARKERS.sub(" ", text_only_query(query))   # an image marker is not a search term
    topic = topic_of(cleaned)
    if topic:
        head = cleaned[: cleaned.rfind(f"({topic})")] if f"({topic})" in cleaned else cleaned
        ordered = _WORDS.findall(topic) + _WORDS.findall(head)
    else:
        ordered = _WORDS.findall(cleaned)
    seen: list[str] = []
    for word in ordered:
        w = word.lower()
        if w not in _STOP_WORDS and w not in seen:
            seen.append(w)
    return seen[:_MAX_QUERY_TERMS]


_TEXT_SEARCH_SQL = text(
    """
    SELECT c.id, c.document_id, d.title, d.version, c.content, c.chunk_metadata,
           -- normalised to 0..1 (flag 32), then weighted by how directly the source speaks to this product's law: the Act and the Rules
           -- first, notifications and guidance next, anything else (the IT Act) last
           ts_rank_cd(to_tsvector('english', c.content), to_tsquery('english', :q), 32)
             * CASE d.source_type WHEN 'dpdp_act' THEN 1.0 WHEN 'dpdp_rules' THEN 1.0 WHEN 'govt_notification' THEN 0.8
                                  WHEN 'govt_guidance' THEN 0.8 ELSE 0.5 END AS rank
    FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id
    WHERE d.is_approved IS TRUE AND to_tsvector('english', c.content) @@ to_tsquery('english', :q)
    ORDER BY rank DESC
    LIMIT :k
    """
)


async def _text_search(query: str, *, db: AsyncSession, top_k: int) -> list[RetrievedChunk]:
    terms = keyword_terms(query)
    if not terms:
        return []
    result = await db.execute(_TEXT_SEARCH_SQL, {"q": " | ".join(terms), "k": top_k + _UNREADABLE_HEADROOM})
    chunks = [
        RetrievedChunk(
            chunk_id=str(row[0]), document_id=str(row[1]), document_title=row[2], document_version=row[3], content=row[4],
            # A rank is not a cosine distance. It is mapped onto the same 0..1 scale (better = smaller) only so results from several
            # queries can be merged and ordered; the distance ceiling is not applied to it.
            distance=max(0.0, 1.0 - min(float(row[6]), 1.0)), metadata=row[5] or {},
        )
        for row in result.all()
    ]
    return [c for c in chunks if is_readable_text(c.content)][:top_k]


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
    stats.update(embed_calls=0, embed_ms=0, search_ms=0, cache_hits=0, knowledge_base_empty=False, mode="embedding")

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
    for query_text in texts:
        if not has_text(query_text) or query_text in embeddings or query_text in pending:
            continue
        cached = _cache_get(query_text)
        if cached is not None:
            embeddings[query_text] = cached
            stats["cache_hits"] += 1
        else:
            pending.append(query_text)

    if pending:
        started = time.perf_counter()
        try:
            stats["embed_calls"] += 1
            vectors = await llm_client.embed(pending, input_type="query")
            if len(vectors) != len(pending):
                raise LLMOutputValidationError(
                    f"Embedding service returned {len(vectors)} vectors for {len(pending)} queries."
                )
            for query_text, vector in zip(pending, vectors, strict=True):
                embeddings[query_text] = vector
                _cache_put(query_text, vector)
        except openai.BadRequestError as exc:
            if "image input" not in str(exc).lower():
                raise
            for query_text, original in ((t, q) for q in queries for t in [text_only_query(q)] if t in pending):
                if query_text in embeddings:
                    continue
                stats["embed_calls"] += 1
                embeddings[query_text] = await _embed_one(original, query_text, llm_client)
        except _EMBEDDING_UNAVAILABLE as exc:
            # The service did not answer within its deadline (hedged requests included). Search the same chunks by keyword instead of
            # giving the analysis up: the provisions are still found and the checks on what the model cites still apply.
            logger.warning("Embedding service unavailable (%s); using stored topic embeddings, then keyword search", type(exc).__name__)
            stats["mode"] = "fallback"
            stats["embedding_error"] = type(exc).__name__
        stats["embed_ms"] = int((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    results: list[list[RetrievedChunk]] = []
    used = {"topic": 0, "keyword": 0}
    for query_text in texts:
        if query_text in embeddings:
            results.append(await _search(embeddings[query_text], db=db, top_k=top_k, max_distance=max_distance))
        elif stats.get("mode") == "fallback" and has_text(query_text):
            # The service did not answer for this query. First choice: the stored embedding of the provision the query is about, which is
            # the same cosine search over the same chunks. Otherwise the words of the query.
            stored = stored_vector(query_text)
            if stored is not None:
                used["topic"] += 1
                results.append(await _search(stored, db=db, top_k=top_k, max_distance=max_distance))
            else:
                used["keyword"] += 1
                results.append(await _text_search(query_text, db=db, top_k=top_k))
        else:
            results.append([])      # nothing but an image: no sentence to retrieve provisions for
    if stats.get("mode") == "fallback":
        stats["mode"] = "topic_embeddings" if not used["keyword"] else "text_search" if not used["topic"] else "topic_embeddings+text_search"
        stats["fallback_topic"], stats["fallback_keyword"] = used["topic"], used["keyword"]
    stats["search_ms"] = int((time.perf_counter() - started) * 1000)
    return results
