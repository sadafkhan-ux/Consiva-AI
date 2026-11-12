"""The shared policy-analysis cache: the same exact question gets the same answer in every organisation, and nothing foreign is ever shown."""
from contextlib import asynccontextmanager

from app.policy import analysis


class _Result:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _Session:
    def __init__(self, value=None, boom=False):
        self.value, self.boom, self.statements, self.committed = value, boom, [], False

    async def execute(self, stmt):
        if self.boom:
            raise RuntimeError("database down")
        self.statements.append(stmt)
        return _Result(self.value)

    async def commit(self):
        self.committed = True


def _factory(session):
    @asynccontextmanager
    async def make():
        yield session
    return make


async def test_a_stored_analysis_is_served_and_never_names_another_scan(monkeypatch):
    stored = {"status": analysis.ANALYSED, "sector": "retail", "requirements": [], "input_hash": "h"}
    monkeypatch.setattr(analysis, "async_session_factory", _factory(_Session(stored)))
    out = await analysis.find_shared("h")
    assert out["status"] == analysis.ANALYSED and out["sector"] == "retail"
    assert out["reused_from_scan"] == "earlier"           # a marker, never another organisation's scan id


async def test_nothing_stored_or_not_analysed_means_nothing_is_reused(monkeypatch):
    monkeypatch.setattr(analysis, "async_session_factory", _factory(_Session(None)))
    assert await analysis.find_shared("h") is None
    monkeypatch.setattr(analysis, "async_session_factory", _factory(_Session({"status": "unavailable"})))
    assert await analysis.find_shared("h") is None


async def test_storing_keeps_the_analysis_without_scan_specific_fields(monkeypatch):
    session = _Session()
    monkeypatch.setattr(analysis, "async_session_factory", _factory(session))
    await analysis.store_shared("h", {"status": analysis.ANALYSED, "sector": "retail", "notes": ["fetched today"], "reused_from_scan": "abc", "input_hash": "h"})
    assert session.committed and len(session.statements) == 1
    kept = session.statements[0].compile().params["result"]
    assert "notes" not in kept and "reused_from_scan" not in kept and kept["sector"] == "retail"


async def test_a_failure_to_store_never_breaks_the_analysis(monkeypatch):
    monkeypatch.setattr(analysis, "async_session_factory", _factory(_Session(boom=True)))
    await analysis.store_shared("h", {"status": analysis.ANALYSED})          # must not raise
