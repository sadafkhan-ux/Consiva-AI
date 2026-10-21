"""An empty cookie lookup table is reported at start-up, and a check that cannot run never stops the API."""

import logging
from contextlib import asynccontextmanager

from app.lookup import health


class _Db:
    def __init__(self, row=None, error=None):
        self._row, self._error = row, error

    async def execute(self, _stmt):
        if self._error:
            raise self._error

        class _R:
            def first(inner):
                return self._row
        return _R()


def _factory(db):
    @asynccontextmanager
    async def session():
        yield db
    return session


async def test_an_empty_table_is_logged_as_an_error(monkeypatch, caplog):
    monkeypatch.setattr(health, "async_session_factory", _factory(_Db(row=None)))

    with caplog.at_level(logging.ERROR):
        assert await health.warn_if_cookie_lookup_empty() is True

    assert "cookie lookup table is EMPTY" in caplog.text and "app.lookup.loader" in caplog.text


async def test_a_loaded_table_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(health, "async_session_factory", _factory(_Db(row=("id",))))

    with caplog.at_level(logging.WARNING):
        assert await health.warn_if_cookie_lookup_empty() is False

    assert caplog.text == ""


async def test_an_unreadable_database_does_not_stop_start_up(monkeypatch):
    monkeypatch.setattr(health, "async_session_factory", _factory(_Db(error=RuntimeError("down"))))

    assert await health.warn_if_cookie_lookup_empty() is False


def test_start_up_runs_the_check():
    import inspect

    from app import main

    assert "warn_if_cookie_lookup_empty" in inspect.getsource(main.lifespan)
