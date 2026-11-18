"""A scan cancelled while it is past its page loop (the Accept and Reject passes, the withdrawal test, the button comparison) returns a full result whose stop_reason is not
"cancelled", and save_scan_result marks a scan "completed". The cancel must still stand: the scan stays cancelled and is never analysed."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.scanner.schemas import ConsentSignalRecord, ScanResult
from app.services import scan_service


def _result(**diagnostics) -> ScanResult:
    return ScanResult(domain="example.com", root_url="https://example.com/", scanner_version="test",
                      consent_signals=ConsentSignalRecord(mechanism_type="none"), scan_diagnostics=diagnostics)


class _Session:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass

    async def get(self, model, ident):
        return self.row


@asynccontextmanager
async def _track(scan_id, stage, *, agent_run_id=None):
    yield {}


def _run(monkeypatch, row, *, cancel_during_crawl, **diagnostics):
    async def crawl(url, **kw):
        if cancel_during_crawl:
            row.status = "cancelled"            # what POST .../cancel does while the scan is running
        return _result(**diagnostics)

    async def save(db, *, scan_id, scan_result, classification):
        row.status = "completed"                 # what save_scan_result does
        return {}

    monkeypatch.setattr(scan_service, "async_session_factory", lambda: _Session(row))
    monkeypatch.setattr(scan_service, "track_stage", _track)
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_running", AsyncMock())
    monkeypatch.setattr(scan_service.scan_repository, "save_scan_result", save)
    monkeypatch.setattr(scan_service.lookup_repository, "load_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(scan_service, "run_scan_isolated", crawl)
    monkeypatch.setattr(scan_service, "classify_scan", MagicMock(return_value=SimpleNamespace(cookies=[], trackers=[], third_party_services=[])))


async def test_a_scan_cancelled_after_the_page_loop_stays_cancelled(monkeypatch):
    row = SimpleNamespace(status="running", scan_options=None, org_id=None)
    _run(monkeypatch, row, cancel_during_crawl=True, stop_reason="queue_exhausted")        # the crawl finished its pages; the cancel came later
    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")
    assert row.status == "cancelled"


async def test_a_scan_cancelled_during_the_page_loop_still_stays_cancelled(monkeypatch):
    row = SimpleNamespace(status="running", scan_options=None, org_id=None)
    _run(monkeypatch, row, cancel_during_crawl=False, stop_reason="cancelled")
    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")
    assert row.status == "cancelled"


async def test_a_scan_nobody_cancelled_completes_as_before(monkeypatch):
    row = SimpleNamespace(status="running", scan_options=None, org_id=None)
    _run(monkeypatch, row, cancel_during_crawl=False, stop_reason="queue_exhausted")
    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")
    assert row.status == "completed"


async def test_a_scan_cancelled_before_it_started_is_not_crawled(monkeypatch):
    row = SimpleNamespace(status="cancelled", scan_options=None, org_id=None)
    _run(monkeypatch, row, cancel_during_crawl=False, stop_reason="queue_exhausted")
    crawl = AsyncMock()
    monkeypatch.setattr(scan_service, "run_scan_isolated", crawl)
    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")
    crawl.assert_not_awaited()
    assert row.status == "cancelled"
