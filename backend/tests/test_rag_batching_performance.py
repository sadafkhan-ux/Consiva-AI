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


async def test_an_unanswered_embedding_falls_back_to_keyword_search_and_says_so(monkeypatch):
    class _Down:
        async def embed(self, *a, **k):
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))

    searched: list[str] = []

    async def fake_text_search(query, *, db, top_k):
        searched.append(query)
        return []

    monkeypatch.setattr(retriever, "_text_search", fake_text_search)
    stats: dict = {}

    out = await retrieve_many(QUERIES, db=_Db(), llm_client=_Down(), top_k=3, stats=stats)

    assert out == [[], [], []] and len(searched) == 3          # every query was searched by keyword, none was dropped
    assert stats["mode"] == "text_search" and stats["embedding_error"] == "APITimeoutError"


async def test_an_error_that_is_not_about_the_service_being_unreachable_is_still_raised():
    class _Broken:
        async def embed(self, *a, **k):
            raise RuntimeError("the embedding model returned something unreadable")

    with pytest.raises(RuntimeError):
        await retrieve_many(QUERIES, db=_Db(), llm_client=_Broken(), top_k=3)


async def test_cached_queries_still_use_the_vector_search_when_the_service_is_down(monkeypatch):
    await retrieve_many(QUERIES[:1], db=_Db(), llm_client=_Embedder(), top_k=3)       # the first query is now cached

    class _Down:
        async def embed(self, *a, **k):
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))

    keyword: list[str] = []

    async def fake_text_search(query, *, db, top_k):
        keyword.append(query)
        return []

    monkeypatch.setattr(retriever, "_text_search", fake_text_search)
    stats: dict = {}
    await retrieve_many(QUERIES, db=_Db(), llm_client=_Down(), top_k=3, stats=stats)

    assert keyword == QUERIES[1:]                                                      # only the uncached ones fell back
    assert stats["cache_hits"] == 1


# ── the keyword search ───────────────────────────────────────────────────────────

def test_keyword_terms_are_the_distinctive_words_without_repeats_or_filler():
    terms = retriever.keyword_terms(
        "19 analytics/marketing cookie(s)/script(s) fired BEFORE any consent interaction (consent under section 6, "
        "processing personal data before consent is given)")
    assert terms[:4] == ["consent", "processing", "personal", "data"]     # the provision in brackets leads, the rule's own wording follows
    assert "analytics" in terms and "cookie" in terms
    assert terms.count("consent") == 1
    assert not {"before", "under", "section"} & set(terms)
    assert len(terms) <= 12


def test_keyword_terms_ignore_an_inline_image_and_a_query_with_no_words():
    assert retriever.keyword_terms(PIXEL) == []
    assert retriever.keyword_terms("19 3 6") == []


class _TextRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _TextDb:
    def __init__(self, rows):
        self.rows, self.params = rows, None

    async def execute(self, _stmt, params=None):
        self.params = params
        return _TextRows(self.rows)


async def test_the_keyword_search_returns_chunks_in_rank_order_and_drops_unreadable_ones():
    english = "A Data Fiduciary shall give notice of the personal data collected and the purpose of processing."
    hindi = "ती”से ऐसा कोई मध्यवती अजभप्रेत है िो अपिे क ं प्यूटर संसािि के उपयोर्कतागओं को एक या अडिक ऑिलाइि"
    rows = [("c1", "d1", "DPDP Act 2023", None, hindi, {}, 0.9), ("c2", "d1", "DPDP Act 2023", "1", english, {"section": "section 5"}, 0.6),
            ("c3", "d2", "DPDP Rules", None, english + " Rule 3.", {}, 0.2), ("c4", "d2", "DPDP Rules", None, english + " Two.", {}, 0.1)]
    db = _TextDb(rows)

    found = await retriever._text_search("notice of personal data collected", db=db, top_k=2)

    assert [c.chunk_id for c in found] == ["c2", "c3"]
    assert found[0].distance < found[1].distance                 # a better rank is a smaller distance
    assert found[0].section == "section 5" and found[0].document_version == "1"
    assert db.params["q"] == "notice | personal | data | collected"
    assert db.params["k"] == 2 + retriever._UNREADABLE_HEADROOM


async def test_the_keyword_search_does_nothing_for_a_query_with_no_words():
    db = _TextDb([])
    assert await retriever._text_search(PIXEL, db=db, top_k=3) == []
    assert db.params is None


# ── the client's own limits ──────────────────────────────────────────────────────

async def _hedge(make, **kw):
    return await llm_client_module.hedged(make, hedge_after=kw.get("hedge_after", 0.05), extra=kw.get("extra", 3), deadline=kw.get("deadline", 2.0))


def _timeout_error():
    return openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))


async def test_a_healthy_call_sends_exactly_one_request():
    calls = []

    async def make():
        calls.append(1)
        return "answer"

    assert await _hedge(make) == "answer" and len(calls) == 1


async def test_a_hung_request_is_rescued_by_a_second_one_sent_alongside_it():
    calls, cancelled = [], []

    async def make():
        calls.append(1)
        n = len(calls)
        if n == 1:
            try:
                await asyncio.sleep(30)              # the endpoint never answers this one
            except asyncio.CancelledError:
                cancelled.append(n)
                raise
        return f"answer from request {n}"

    started = time.perf_counter()
    result = await _hedge(make)

    assert result == "answer from request 2"
    assert time.perf_counter() - started < 1.0       # not the 30s the hung request would have taken, nor a 6s timeout
    assert cancelled == [1]                          # the hung request is cancelled, not left running


async def test_only_a_bounded_number_of_requests_is_ever_sent_and_all_failing_raises_a_timeout():
    calls = []

    async def make():
        calls.append(1)
        await asyncio.sleep(30)

    with pytest.raises(openai.APITimeoutError):
        await _hedge(make, extra=2, deadline=0.4)

    assert len(calls) == 3                           # the first request and two hedges, never more


async def test_a_refusal_is_raised_at_once_and_not_hedged():
    calls = []

    async def make():
        calls.append(1)
        raise openai.BadRequestError("This model does not support image input as query", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)

    with pytest.raises(openai.BadRequestError):
        await _hedge(make)

    assert len(calls) == 1


async def test_a_failed_request_is_replaced_by_another_without_waiting_for_the_hedge_delay():
    calls = []

    async def make():
        calls.append(1)
        if len(calls) == 1:
            raise _timeout_error()
        return "ok"

    started = time.perf_counter()
    assert await _hedge(make, hedge_after=5.0) == "ok"
    assert time.perf_counter() - started < 1.0


async def test_the_embedding_request_is_sent_without_the_sdk_retries_and_with_the_overall_deadline(monkeypatch):
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

    # the SDK's own retries are replaced by the hedging; the timeout is the deadline for one request
    assert seen == {"timeout": 12.0, "max_retries": 0}


def test_the_worst_case_for_one_embedding_call_is_bounded_by_its_deadline_not_by_attempts_times_timeout():
    from app.config import get_settings

    s = get_settings()
    assert s.embed_timeout_seconds <= 15, "one deadline for the whole call, hedges included"
    assert s.embed_hedge_after_seconds < 3 and s.embed_max_retries >= 2    # a hang is rescued in seconds, by more than one extra request
    assert s.embed_timeout_seconds < 60, "the SDK default was 60s, and the production request hung for 121.9s"


async def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(retriever, "_EMBED_CACHE_MAX", 2)

    await retrieve_many(QUERIES, db=_Db(), llm_client=_Embedder(), top_k=3)

    assert len(retriever._EMBED_CACHE) == 2


# ── the client's own limits ──────────────────────────────────────────────────────

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


# ── stored topic embeddings: the first fallback, before any keyword search ───────────────────────────

import json  # noqa: E402

from app.rag import topic_embeddings  # noqa: E402
from app.rules.consent_rules import RULE_DPDP_TOPIC_HINTS  # noqa: E402


@pytest.fixture
def stored(monkeypatch):
    """Two stored topic vectors, standing in for the shipped file."""
    vectors = {"consent under section 6, processing personal data before consent is given": [1.0, 0.0],
               "withdrawal of consent under sub-section (4) of section 6": [0.0, 1.0]}
    monkeypatch.setattr(topic_embeddings, "_load", lambda: vectors)
    return vectors


def test_the_topic_of_a_query_is_the_provision_in_its_closing_brackets(stored):
    assert topic_embeddings.topic_of("61 trackers fired before consent. (consent under section 6, processing personal data before consent is given)") == \
        "consent under section 6, processing personal data before consent is given"
    assert topic_embeddings.topic_of("a query with no provision") is None
    assert topic_embeddings.topic_of("a query that mentions (something) in the middle and ends plainly") is None


def test_a_provision_that_has_brackets_of_its_own_is_matched_whole(stored):
    query = "Tracking continued after Reject. (withdrawal of consent under sub-section (4) of section 6)"
    assert topic_embeddings.topic_of(query) == "withdrawal of consent under sub-section (4) of section 6"
    assert topic_embeddings.stored_vector(query) == [0.0, 1.0]


def test_a_query_whose_provision_is_not_stored_gets_no_stored_vector(stored):
    assert topic_embeddings.stored_vector("a finding (notice under section 5)") is None
    assert topic_embeddings.stored_vector("a finding with no brackets") is None


def test_the_shipped_file_covers_every_provision_the_rules_name_at_the_width_of_the_vector_column():
    from app.db.models import EMBED_DIM

    data = json.loads(topic_embeddings.PATH.read_text(encoding="utf-8"))
    assert data["model"] and data["dimensions"] == EMBED_DIM
    needed = {topic_embeddings._normalise(h) for h in RULE_DPDP_TOPIC_HINTS.values()}
    assert needed <= set(data["topics"]), "regenerate with: python -m app.rag.topic_embeddings"
    assert all(len(v) == data["dimensions"] for v in data["topics"].values())


def test_a_file_made_with_another_model_is_ignored(monkeypatch, tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"model": "some-other-model", "dimensions": 1024, "topics": {"x": [0.0] * 1024}}), encoding="utf-8")
    monkeypatch.setattr(topic_embeddings, "PATH", f)
    topic_embeddings.clear_cache()
    try:
        assert topic_embeddings._load() == {}
    finally:
        topic_embeddings.clear_cache()


async def test_when_the_service_is_down_a_stored_provision_is_searched_by_vector_and_the_rest_by_keyword(monkeypatch, stored):
    class _Down:
        async def embed(self, *a, **k):
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))

    searched_with: list = []
    keyword: list[str] = []

    async def fake_search(vector, *, db, top_k, max_distance):
        searched_with.append(vector)
        return []

    async def fake_text_search(query, *, db, top_k):
        keyword.append(query)
        return []

    monkeypatch.setattr(retriever, "_search", fake_search)
    monkeypatch.setattr(retriever, "_text_search", fake_text_search)
    stats: dict = {}
    queries = ["61 trackers fired before consent. (consent under section 6, processing personal data before consent is given)",
               "Tracking continued after Reject. (withdrawal of consent under sub-section (4) of section 6)",
               "3 scripts matched no vendor."]

    await retrieve_many(queries, db=_Db(), llm_client=_Down(), top_k=3, stats=stats)

    assert searched_with == [[1.0, 0.0], [0.0, 1.0]]            # the two provisions that are stored
    assert keyword == ["3 scripts matched no vendor."]           # the one that names none
    assert stats["mode"] == "topic_embeddings+text_search" and (stats["fallback_topic"], stats["fallback_keyword"]) == (2, 1)


async def test_the_mode_is_topic_embeddings_when_every_query_has_a_stored_provision(monkeypatch, stored):
    class _Down:
        async def embed(self, *a, **k):
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/v1/embeddings"))

    async def fake_search(vector, *, db, top_k, max_distance):
        return []

    monkeypatch.setattr(retriever, "_search", fake_search)
    stats: dict = {}
    await retrieve_many(["x. (consent under section 6, processing personal data before consent is given)"], db=_Db(), llm_client=_Down(), top_k=3, stats=stats)
    assert stats["mode"] == "topic_embeddings"


def test_the_query_used_when_no_rule_matched_has_a_stored_vector_too():
    assert topic_embeddings.topic_of(topic_embeddings.GENERAL_QUERY) == topic_embeddings._normalise(topic_embeddings.GENERAL_QUERY)
    data = json.loads(topic_embeddings.PATH.read_text(encoding="utf-8"))
    assert topic_embeddings._normalise(topic_embeddings.GENERAL_QUERY) in data["topics"]
    from app.agents.consent_agent.nodes.retrieve_rag import _queries
    assert _queries([]) == [topic_embeddings.GENERAL_QUERY]
