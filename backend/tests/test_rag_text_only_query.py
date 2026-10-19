"""A retrieval query is text. The configured embedding model is a vision-language one and rejects an image as a
QUERY ("This model does not support image input as query", HTTP 400), so an inline image in a query is replaced
by a text marker before embedding. Reproduced live against the real endpoint with a bare data: URI.

No database and no network: the embedding client is a fake.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.agents.consent_agent.nodes import retrieve_rag
from app.core.exceptions import LLMOutputValidationError
from app.rag import retriever
from app.rag.retriever import has_text, text_only_query

PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


class _Rows:
    def all(self):
        return []


class _Db:
    async def execute(self, _stmt):
        return _Rows()


class _Embedder:
    def __init__(self, *, reject=lambda text: False):
        self.sent: list[str] = []
        self._reject = reject

    async def embed(self, texts, *, input_type):
        assert input_type == "query"
        self.sent.extend(texts)
        if any(self._reject(t) for t in texts):
            request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/embeddings")
            response = httpx.Response(400, request=request, json={"error": "This model does not support image input as query"})
            raise openai.BadRequestError(
                "Error code: 400 - {'error': 'This model does not support image input as query'}",
                response=response, body=None,
            )
        return [[0.0] * 1024 for _ in texts]


# ── the sanitiser ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    (PIXEL, "[inline image]"),
    (f"Tracker beacon {PIXEL} fired before any consent", "Tracker beacon [inline image] fired before any consent"),
    ("  data:image/gif;base64,R0lGODlhAQABAAAAACw=  ", "[inline image]"),
    ('A tracker <img src="https://x.example/p.gif" width=1> fired', "A tracker [image] fired"),
    ("No privacy policy page was discovered during the crawl.", "No privacy policy page was discovered during the crawl."),
    ("multi\n  line\ttext", "multi line text"),
])
def test_inline_images_become_text_markers_and_ordinary_text_is_untouched(query, expected):
    assert text_only_query(query) == expected


def test_image_urls_are_ordinary_text_by_default_and_dropped_only_in_the_strict_form():
    q = "Third-party request to https://images.unsplash.com/photo-1.jpg before consent"
    assert text_only_query(q) == q
    assert text_only_query(q, strip_urls=True) == "Third-party request to [link] before consent"


@pytest.mark.parametrize("query,expected", [
    ("[inline image]", False),
    ("[image] [inline image]", False),
    ("", False),
    ("[inline image] fired before consent", True),
    ("No privacy policy page was found.", True),
])
def test_has_text_ignores_the_markers(query, expected):
    assert has_text(query) is expected


# ── retrieve() ───────────────────────────────────────────────────────────────────

async def test_an_image_only_query_never_reaches_the_embedding_model_and_returns_nothing():
    embedder = _Embedder(reject=lambda t: t.startswith("data:image"))

    out = await retriever.retrieve(PIXEL, db=_Db(), llm_client=embedder, top_k=3)

    assert out == []
    assert embedder.sent == [], "the API was never called, so it could not return the 400"


async def test_a_query_with_an_image_inside_is_embedded_as_its_text():
    embedder = _Embedder(reject=lambda t: t.startswith("data:image"))

    await retriever.retrieve(f"Beacon {PIXEL} fired before consent", db=_Db(), llm_client=embedder, top_k=3)

    assert embedder.sent == ["Beacon [inline image] fired before consent"]


async def test_an_ordinary_query_is_sent_exactly_as_it_was():
    embedder = _Embedder()
    await retriever.retrieve("No privacy policy page was discovered.", db=_Db(), llm_client=embedder, top_k=3)
    assert embedder.sent == ["No privacy policy page was discovered."]


async def test_if_the_api_still_reports_image_input_one_stricter_text_only_attempt_is_made():
    # The model decides a plain URL in the sentence is an image: the first attempt is refused, the second
    # (URLs dropped) goes through.
    embedder = _Embedder(reject=lambda t: "https://" in t)

    await retriever.retrieve("Request to https://cdn.example/a.jpg fired before consent", db=_Db(), llm_client=embedder, top_k=3)

    assert embedder.sent == [
        "Request to https://cdn.example/a.jpg fired before consent",
        "Request to [link] fired before consent",
    ]


async def test_a_refusal_that_cannot_be_worked_around_is_a_clear_error_not_an_empty_result():
    embedder = _Embedder(reject=lambda t: True)

    with pytest.raises(LLMOutputValidationError, match="image input"):
        await retriever.retrieve("fired before consent", db=_Db(), llm_client=embedder, top_k=3)


async def test_an_unrelated_bad_request_is_not_swallowed():
    class _Other:
        async def embed(self, *a, **k):
            request = httpx.Request("POST", "https://x/v1/embeddings")
            raise openai.BadRequestError("Error code: 400 - model not found", response=httpx.Response(400, request=request), body=None)

    with pytest.raises(openai.BadRequestError):
        await retriever.retrieve("fired before consent", db=_Db(), llm_client=_Other(), top_k=3)


# ── the Consent Agent's retrieval step ───────────────────────────────────────────

def _state(*summaries):
    return SimpleNamespace(
        scan_id=str(uuid.uuid4()), agent_run_id=str(uuid.uuid4()),
        rule_findings=[{"summary": s} for s in summaries],
    )


@pytest.fixture
def wired(monkeypatch):
    queried: list[str] = []
    metas: list[dict] = []

    async def fake_retrieve_many(queries, **kwargs):
        queried.extend(queries)
        return [[] for _ in queries]

    @asynccontextmanager
    async def tracked(scan_id, stage, **kw):
        meta: dict = {}
        metas.append(meta)
        yield meta

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(retrieve_rag, "retrieve_many", fake_retrieve_many)
    monkeypatch.setattr(retrieve_rag, "track_stage", tracked)
    monkeypatch.setattr(retrieve_rag, "async_session_factory", session)
    monkeypatch.setattr(retrieve_rag, "NvidiaLLMClient", lambda: object())
    return SimpleNamespace(queried=queried, metas=metas)


async def test_the_step_sends_text_and_records_how_many_queries_were_cleaned(wired):
    state = _state("19 trackers fired before consent.", f"Beacon {PIXEL} fired.")

    await retrieve_rag.retrieve_rag(state)

    assert wired.queried == ["19 trackers fired before consent.", "Beacon [inline image] fired."]
    assert wired.metas[0]["queries_text_only"] == 1


async def test_an_image_only_finding_falls_back_to_the_general_query_instead_of_retrieving_nothing(wired):
    await retrieve_rag.retrieve_rag(_state(PIXEL))

    assert wired.queried == ["General DPDP Act consent, cookie, and website tracking compliance requirements"]


async def test_a_scan_with_no_rule_findings_still_uses_the_general_query(wired):
    await retrieve_rag.retrieve_rag(_state())
    assert len(wired.queried) == 1 and wired.queried[0].startswith("General DPDP Act")


async def test_the_rule_findings_themselves_are_not_modified(wired):
    state = _state(f"Beacon {PIXEL} fired.")

    await retrieve_rag.retrieve_rag(state)

    assert state.rule_findings[0]["summary"] == f"Beacon {PIXEL} fired.", "evidence is untouched; only the query text changed"
