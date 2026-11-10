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
# LLM usage tracking (app/llm/usage_tracking.py): tests never write to the real usage log or to a database. Without this the unit tests that call the
# shared client appended test-model rows to logs/llm_usage.jsonl, and they made up most of that file.
os.environ["CONSIVA_DISABLE_USAGE_DB"] = "1"
os.environ.setdefault("SCANNER_TLS_ASSESSMENT", "false")     # the tests never open a connection to a real site
os.environ.setdefault("CONSIVA_LLM_USAGE_LOG", os.path.join(__import__("tempfile").gettempdir(), "consiva-test-llm-usage.jsonl"))

import pytest


@pytest.fixture(autouse=True)
def _fresh_query_embedding_cache():
    """The retrieval layer caches query embeddings per process; a test must never see another test's."""
    from app.rag.retriever import clear_query_embedding_cache

    clear_query_embedding_cache()
    yield
    clear_query_embedding_cache()


@pytest.fixture(autouse=True)
def _report_qc_sees_stand_in_pdfs(request, monkeypatch):
    """Tests that exercise the report cache hand it stand-in bytes (b"%PDF-new"), which are not real PDFs; the printed-PDF quality gate is checked in test_report_qc.py with real ones."""
    if request.module.__name__.endswith("test_report_qc"):
        return
    from app.services import report_qc

    monkeypatch.setattr(report_qc, "check_pdf", lambda pdf, variant, domain=None: [])
    if request.module.__name__.endswith("test_report_cache_invalidation"):
        return                      # these tests are about the real web-app signature
    from app.services import report_cache_service

    async def known_build(settings=None):
        return "test-build"

    monkeypatch.setattr(report_cache_service, "frontend_signature", known_build)
