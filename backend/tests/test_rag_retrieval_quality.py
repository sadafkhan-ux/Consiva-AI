"""Retrieval-quality integration test for app.rag.retriever.retrieve — deliberately
NOT mocked. Per the project's testing policy ("mock NVIDIA in unit tests, keep live
NVIDIA testing as integration test"), this hits the real NVIDIA embeddings API and the
real live Supabase Postgres/pgvector database configured in .env, and reports actual
measured latencies plus actual retrieved chunk content so relevance can be sanity-checked
by a human reading the test report — nothing here is estimated or fabricated.

Environment note: tests/conftest.py sets NVIDIA_API_KEY / DATABASE_URL / etc. as real
process env vars (via os.environ.setdefault(...)) so the rest of the unit-test suite
never touches live services. Because pydantic-settings' precedence is
init-kwargs > process-env > .env-file, those env vars would win over the real .env
values for anything using the normal app.config.get_settings(). Rather than popping
those env vars / clearing get_settings()'s global lru_cache (which previously polluted
every OTHER test file collected afterward in the same pytest run — os.environ and the
lru_cache are both process-wide, so a mutation here leaked into unrelated tests), this
file reads backend/.env directly (bypassing os.environ entirely, matching
test_prompt_injection_probe.py's _real_settings() pattern) and builds its own Settings
+ its own throwaway DB engine/session, never touching app.db.session's shared
module-level singleton either.
"""

import sys
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import KnowledgeDocument
from app.llm.client import NvidiaLLMClient
from app.rag.retriever import retrieve

QUERY = "Consent withdrawal and notice requirements"
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _real_settings() -> Settings | None:
    values = dotenv_values(_ENV_FILE)
    api_key = values.get("NVIDIA_API_KEY")
    database_url = values.get("DATABASE_URL")
    if not api_key or not database_url:
        return None
    return Settings(
        nvidia_api_key=api_key,
        nvidia_api_base_url=values.get("NVIDIA_API_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        nvidia_llm_model=values.get("NVIDIA_LLM_MODEL", "unused-in-this-test"),
        nvidia_embed_model=values.get("NVIDIA_EMBED_MODEL", "unused-in-this-test"),
        nvidia_embed_dimensions=int(values.get("NVIDIA_EMBED_DIMENSIONS", "1024")),
        supabase_url=values.get("SUPABASE_URL", "http://localhost"),
        supabase_service_role_key="unused-in-this-test",
        database_url=database_url,
        supabase_jwt_secret="unused-in-this-test",
        app_env="development",
    )


pytestmark = pytest.mark.skipif(
    _real_settings() is None,
    reason="live NVIDIA API key / DATABASE_URL not configured in backend/.env; this test needs real infra",
)


def _safe_print(s: str) -> None:
    """The source document (DPDP Rules) is a bilingual Hindi/English Indian gazette
    notification, so retrieved chunk content can contain Devanagari text. Windows'
    default console codepage (cp1252) can't encode that and would crash a plain
    print() -- replace unencodable characters instead of failing the test over a
    terminal-encoding limitation unrelated to what's being tested."""
    encoding = sys.stdout.encoding or "utf-8"
    print(s.encode(encoding, errors="replace").decode(encoding))


class _TimingLLMClient(NvidiaLLMClient):
    """Real NvidiaLLMClient (real network calls) with the `embed` call individually
    timed, so embedding-generation latency can be isolated from the DB round trip
    inside retrieve() without needing to duplicate retrieve()'s query logic here."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.embed_latency_s: float | None = None

    async def embed(self, texts, *, input_type):
        start = time.monotonic()
        result = await super().embed(texts, input_type=input_type)
        self.embed_latency_s = time.monotonic() - start
        return result


async def test_retrieve_returns_relevant_approved_chunks_from_live_db():
    settings = _real_settings()
    assert settings is not None  # guaranteed by pytestmark skipif above

    llm_client = _TimingLLMClient(settings=settings)

    # A throwaway engine/session bound to the real DATABASE_URL -- deliberately NOT
    # app.db.session.async_session_factory, since that module-level singleton may
    # already be bound (by an earlier-imported test module) to conftest.py's dummy
    # DATABASE_URL, and there is no way to change an already-constructed engine.
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args={"timeout": 5})
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            # Ground truth: what's actually ingested right now, so the document_title
            # assertion below checks against real data instead of an assumed title.
            ingested_titles = set((await db.execute(select(KnowledgeDocument.title))).scalars().all())
            assert ingested_titles, (
                "knowledge_documents is empty in the live DB -- nothing has been ingested, "
                "so a retrieval-quality test can't run. Ingest at least one approved "
                "document first."
            )

            total_start = time.monotonic()
            results = await retrieve(QUERY, db=db, llm_client=llm_client, top_k=4)
            total_latency_s = time.monotonic() - total_start
    finally:
        await engine.dispose()

    embed_latency_s = llm_client.embed_latency_s
    assert embed_latency_s is not None, "retrieve() never called llm_client.embed -- embedding step was skipped"
    db_query_latency_s = total_latency_s - embed_latency_s

    print(f"\n--- RAG retrieval quality: query={QUERY!r} top_k=4 ---")
    print(f"embedding generation latency: {embed_latency_s * 1000:.1f} ms")
    print(f"DB query latency (approx, total - embed): {db_query_latency_s * 1000:.1f} ms")
    print(f"total retrieve() wall-clock latency: {total_latency_s * 1000:.1f} ms")
    print(f"chunks returned: {len(results)}")
    for i, chunk in enumerate(results):
        preview = chunk.content[:200].replace("\n", " ")
        _safe_print(f"  [{i}] distance={chunk.distance:.4f} doc={chunk.document_title!r} section={chunk.section!r}")
        _safe_print(f"      content: {preview!r}")

    assert len(results) >= 1, "retrieve() returned no chunks for a query against a populated knowledge base"

    for chunk in results:
        assert chunk.document_title, "retrieved chunk has an empty document_title"
        assert chunk.document_title in ingested_titles, (
            f"retrieved document_title {chunk.document_title!r} does not match any title "
            f"currently in knowledge_documents ({ingested_titles!r})"
        )
        assert chunk.content.strip(), "retrieved chunk has empty content"
        assert isinstance(chunk.distance, float)
