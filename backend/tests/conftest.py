import os

from tests.live_db import PLACEHOLDER_DSN

# Settings has several required fields (real secrets in real deployments) — tests never
# touch a live NVIDIA/Supabase endpoint, so dummy values just need to satisfy validation.
os.environ.setdefault("NVIDIA_API_KEY", "test-key")
os.environ.setdefault("NVIDIA_LLM_MODEL", "test-model")
os.environ.setdefault("NVIDIA_EMBED_MODEL", "test-embed-model")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
# The live-database tests now consult the environment, so they have to be able to tell
# this placeholder apart from a real DSN. tests/live_db.py owns the value for that
# reason -- see its PLACEHOLDER_DSN comment.
os.environ.setdefault("DATABASE_URL", PLACEHOLDER_DSN)
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")
os.environ.setdefault("APP_ENV", "development")

import pytest


@pytest.fixture(autouse=True)
def _fresh_query_embedding_cache():
    """The retrieval layer caches query embeddings per process; a test must never see another test's."""
    from app.rag.retriever import clear_query_embedding_cache

    clear_query_embedding_cache()
    yield
    clear_query_embedding_cache()
