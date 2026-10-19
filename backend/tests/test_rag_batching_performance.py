"""Retrieval latency, measured on a real scan (ac537cef, gignaati.com): rag_retrieval took 216.78s for THREE queries.

The worker log showed why. Each query was embedded in its own request, one after another, and the hosted embedding
endpoint hung on some of them: "Retrying request" appeared after each 60s client timeout (the SDK defaults are a 60s
timeout and 2 silent retries), so the three calls took 121.9s, 61.0s and 34.0s. The same scan's knowledge base had
no chunks at all, so every one of those waits bought nothing.

What these tests pin down: one embedding request per scan instead of one per query, a bounded timeout and retry
count, no embedding at all when there is nothing to compare with, and unchanged results per query.
No database and no network.
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.agents.consent_agent.nodes import retrieve_rag
from app.llm import client as llm_client_module
from app.rag import retriever
from app.rag.retriever import retrieve_many

PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
QUERIES = [
    "19 analytics/marketing cookie(s)/script(s) fired BEFORE any consent interaction.",
    "Tracking cookies/scripts were detected but no dedicated cookie policy was found.",
    "3 script(s)/cookie(s) did not match any known vendor signature.",
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return []


class _Db:
    """First statement: the knowledge-base check. Later ones: the vector searches (always empty here)."""

    def __init__(self, *, has_chunks=True):
        self.has_chunks = has_chunks
        self.statements = 0

    async def execute(self, _stmt):
        self.statements += 1
        if self.statements == 1:
            return _Result([("chunk",)] if self.has_chunks else [])
        return _Result([])


class _Embedder:
    def __init__(self, *, delay=0.0, refuse_batches=False):
        self.calls: list[list[str]] = []
        self._delay = delay
        self._refuse_batches = refuse_batches

    async def embed(self, texts, *, input_type):
        assert input_type == "query"
        self.calls.append(list(texts))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._refuse_batches and len(texts) > 1:
            request = httpx.Request("POST", "https://x/v1/embeddings")
            raise openai.BadRequestError(
                "Error code: 400 - {'error': 'This model does not support image input as query'}",
                response=httpx.Response(400, request=request), body=None,
            )
        return [[float(len(t))] + [0.0] * 1023 for t in texts]


async def test_all_queries_are_embedded_in_one_request():
    embedder = _Embedder()
    stats: dict = {}

    out = await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3, stats=stats)

    assert embedder.calls == [QUERIES], "one request carrying every query, not one request each"
    assert out == [[], [], []]
    assert stats["embed_calls"] == 1 and stats["cache_hits"] == 0 and stats["knowledge_base_empty"] is False
    assert stats["embed_ms"] >= 0 and stats["search_ms"] >= 0


async def test_one_slow_request_costs_one_wait_not_three():
    # The shape of the production failure: every embedding request is slow. One batched request pays that once.
    embedder = _Embedder(delay=0.2)

    started = time.perf_counter()
    await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3)
    elapsed = time.perf_counter() - started

    assert len(embedder.calls) == 1
    assert elapsed < 0.45, f"3 serial requests would take >= 0.6s; took {elapsed:.2f}s"


async def test_a_repeated_query_is_not_embedded_again():
    embedder = _Embedder()
    await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3)

    stats: dict = {}
    await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3, stats=stats)

    assert len(embedder.calls) == 1, "the second scan reused the cached embeddings"
    assert stats["embed_calls"] == 0 and stats["cache_hits"] == 3


async def test_only_the_new_queries_are_sent_when_some_are_cached():
    embedder = _Embedder()
    await retrieve_many(QUERIES[:2], db=_Db(), llm_client=embedder, top_k=3)

    await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3)

    assert embedder.calls[1] == [QUERIES[2]]


async def test_duplicate_queries_in_one_call_are_sent_once_and_results_keep_their_positions():
    embedder = _Embedder()

    out = await retrieve_many([QUERIES[0], QUERIES[1], QUERIES[0]], db=_Db(), llm_client=embedder, top_k=3)

    assert embedder.calls == [[QUERIES[0], QUERIES[1]]]
    assert len(out) == 3


async def test_an_image_only_query_is_skipped_and_the_others_still_retrieve_in_order():
    embedder = _Embedder()
    db = _Db()

    out = await retrieve_many([QUERIES[0], PIXEL, QUERIES[1]], db=db, llm_client=embedder, top_k=3)

    assert embedder.calls == [[QUERIES[0], QUERIES[1]]], "the image never reaches the embedding model"
    assert len(out) == 3 and out[1] == []
    assert db.statements == 1 + 2, "a knowledge-base check, then one search per real query"


async def test_each_query_is_still_searched_on_its_own():
    db = _Db()

    await retrieve_many(QUERIES, db=db, llm_client=_Embedder(), top_k=3)

    assert db.statements == 1 + len(QUERIES), "per-violation retrieval is preserved"


async def test_an_empty_knowledge_base_skips_embedding_and_says_so():
    embedder = _Embedder()
    stats: dict = {}

    out = await retrieve_many(QUERIES, db=_Db(has_chunks=False), llm_client=embedder, top_k=3, stats=stats)

    assert embedder.calls == [] and out == [[], [], []]
    assert stats["knowledge_base_empty"] is True and stats["embed_calls"] == 0


async def test_if_the_batch_is_refused_as_an_image_each_query_falls_back_to_its_own_request():
    embedder = _Embedder(refuse_batches=True)

    out = await retrieve_many(QUERIES, db=_Db(), llm_client=embedder, top_k=3)

    assert embedder.calls[0] == QUERIES and embedder.calls[1:] == [[q] for q in QUERIES]
    assert out == [[], [], []]


async def test_an_unrelated_embedding_failure_is_not_swallowed():
    class _Down:
        async def embed(self, *a, **k):
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))

    with pytest.raises(openai.APITimeoutError):
        await retrieve_many(QUERIES, db=_Db(), llm_client=_Down(), top_k=3)


async def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(retriever, "_EMBED_CACHE_MAX", 2)

    await retrieve_many(QUERIES, db=_Db(), llm_client=_Embedder(), top_k=3)

    assert len(retriever._EMBED_CACHE) == 2


# ── the client's own limits ──────────────────────────────────────────────────────

async def test_the_embedding_request_has_a_short_timeout_and_one_retry(monkeypatch):
    seen: dict = {}

    class _Embeddings:
        async def create(self, **kwargs):
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.0] * 1024)])

    def with_options(self, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(embeddings=_Embeddings())

    monkeypatch.setattr(openai.AsyncOpenAI, "with_options", with_options)
    client = llm_client_module.NvidiaLLMClient()

    await client.embed(["a sentence"], input_type="query")

    assert seen == {"timeout": 6.0, "max_retries": 2}, "the SDK default was 60s and 2 silent retries"


def test_the_worst_case_for_one_embedding_request_is_far_below_the_measured_production_wait():
    from app.config import get_settings

    s = get_settings()
    worst_case = s.embed_timeout_seconds * (s.embed_max_retries + 1)
    assert worst_case <= 20, f"{worst_case}s; the production request hung for 121.9s"


# ── the node records what it spent ───────────────────────────────────────────────

async def test_the_stage_records_embedding_and_search_timings(monkeypatch):
    metas: list[dict] = []

    @asynccontextmanager
    async def tracked(scan_id, stage, **kw):
        meta: dict = {}
        metas.append(meta)
        yield meta

    @asynccontextmanager
    async def session():
        yield _Db(has_chunks=False)

    monkeypatch.setattr(retrieve_rag, "track_stage", tracked)
    monkeypatch.setattr(retrieve_rag, "async_session_factory", session)
    monkeypatch.setattr(retrieve_rag, "NvidiaLLMClient", lambda: _Embedder())
    state = SimpleNamespace(
        scan_id=str(uuid.uuid4()), agent_run_id=str(uuid.uuid4()),
        rule_findings=[{"summary": q} for q in QUERIES],
    )

    out = await retrieve_rag.retrieve_rag(state)

    assert out == {"rag_chunks": []}
    assert set(metas[0]["timings"]) == {"embed_calls", "embed_ms", "search_ms", "cache_hits"}
    assert metas[0]["knowledge_base_empty"] is True, "an empty knowledge base is visible on the stage"
