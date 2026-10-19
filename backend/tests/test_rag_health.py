"""An empty regulatory knowledge base is reported at start-up, and a check that cannot run never stops the API."""

import logging
from contextlib import asynccontextmanager

from app.rag import health


def _session(has_chunks=True, error=None):
    @asynccontextmanager
    async def session():
        yield object()
    return session


async def test_an_empty_knowledge_base_is_logged_as_an_error(monkeypatch, caplog):
    async def empty(db):
        return False

    monkeypatch.setattr(health, "async_session_factory", _session())
    monkeypatch.setattr(health, "_knowledge_base_has_chunks", empty)

    with caplog.at_level(logging.ERROR):
        assert await health.warn_if_knowledge_base_empty() is True

    assert "NO approved chunks" in caplog.text and "cannot cite" in caplog.text


async def test_a_populated_knowledge_base_logs_nothing(monkeypatch, caplog):
    async def populated(db):
        return True

    monkeypatch.setattr(health, "async_session_factory", _session())
    monkeypatch.setattr(health, "_knowledge_base_has_chunks", populated)

    with caplog.at_level(logging.WARNING):
        assert await health.warn_if_knowledge_base_empty() is False

    assert caplog.text == ""


async def test_an_unreadable_database_does_not_stop_start_up(monkeypatch):
    async def broken(db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(health, "async_session_factory", _session())
    monkeypatch.setattr(health, "_knowledge_base_has_chunks", broken)

    assert await health.warn_if_knowledge_base_empty() is False


def test_start_up_runs_the_check():
    import inspect

    from app import main

    assert "warn_if_knowledge_base_empty" in inspect.getsource(main.lifespan)
