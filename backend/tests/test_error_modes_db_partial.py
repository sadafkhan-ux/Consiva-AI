"""Error-mode tests for the scan pipeline:

1. DB-unavailable-mid-scan: does a SQLAlchemy OperationalError raised at each point in
   `app.services.scan_service.execute_scan_and_persist` actually get turned into a
   "failed" scan row (via `scan_repository.mark_scan_failed`), rather than leaving the
   row stuck in "running" forever? Every stage -- website_scan/classification (inside
   the main try/except) and the final save_scan_result/"data_structuring" stage (now
   wrapped in its own try/except) -- is covered.

2. Partial-crawl-failure: does one page failing mid-crawl in
   `app.scanner.crawler._crawl_full_site` abort the whole scan (discarding evidence
   already gathered from pages that succeeded), or does it get skipped while the rest
   of the evidence is kept? Exercised against the real crawl loop with a fake
   Playwright BrowserContext/Page (no real browser, no real DB).

Tests assert the correct/desired contract; a failure here reports a real gap.
"""

import uuid
from collections import deque
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.rules.consent_rules import RulesClassification
from app.scanner.crawler import _crawl_full_site
from app.scanner.schemas import ConsentSignalRecord, ScanResult
from app.services import scan_service

# ---------------------------------------------------------------------------
# 1. DB unavailable mid-scan
# ---------------------------------------------------------------------------


class _FakeAsyncSession:
    """Stands in for `async with async_session_factory() as db: ...` without opening
    any real connection. Individual repository calls made "through" it are separately
    mocked below, so nothing here ever touches SQL."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        pass


def _fake_session_factory():
    return _FakeAsyncSession()


@asynccontextmanager
async def _passthrough_track_stage(scan_id, stage, *, agent_run_id=None):
    """Real `track_stage` opens its own separate DB sessions (via
    app.observability.stage_tracker's own `async_session_factory` import) to record
    stage start/finish -- irrelevant to what's under test here and best-effort/
    swallowed on failure in the real code anyway. This stand-in keeps the
    yield/raise semantics (any exception in the block still propagates) without any
    DB I/O, so the test isn't at the mercy of a 5s connect-timeout against a fake
    DATABASE_URL."""
    yield {}


def _fake_scan_result() -> ScanResult:
    return ScanResult(
        domain="example.com",
        root_url="https://example.com/",
        scanner_version="test",
        consent_signals=ConsentSignalRecord(mechanism_type="none"),
    )


def _fake_classification() -> RulesClassification:
    return RulesClassification(cookies=[], trackers=[], third_party_services=[])


def _patch_common(monkeypatch, *, run_scan_isolated=None, classify_scan=None):
    monkeypatch.setattr(scan_service, "async_session_factory", _fake_session_factory)
    monkeypatch.setattr(scan_service, "track_stage", _passthrough_track_stage)
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_running", AsyncMock())
    monkeypatch.setattr(scan_service.lookup_repository, "load_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        scan_service,
        "run_scan_isolated",
        run_scan_isolated or AsyncMock(return_value=_fake_scan_result()),
    )
    monkeypatch.setattr(
        scan_service, "classify_scan", classify_scan or MagicMock(return_value=_fake_classification())
    )


async def test_db_outage_during_crawl_stage_marks_scan_failed(monkeypatch):
    """Control case: a DB/backend outage surfacing *inside* the try/except (here, via
    the website_scan stage's `run_scan_isolated` call) IS correctly turned into a
    "failed" scan row before the exception is re-raised. This confirms the
    mark_scan_failed safety net works as designed for stages inside the try block --
    the contrast is what makes the next test's gap meaningful rather than an
    artifact of the mocking."""
    scan_id = uuid.uuid4()
    outage = OperationalError("SELECT 1", {}, Exception("connection to server was lost"))

    mark_failed_mock = AsyncMock()
    _patch_common(monkeypatch, run_scan_isolated=AsyncMock(side_effect=outage))
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_failed", mark_failed_mock)
    monkeypatch.setattr(scan_service.scan_repository, "save_scan_result", AsyncMock())

    with pytest.raises(OperationalError):
        await scan_service.execute_scan_and_persist(scan_id, "https://example.com/")

    mark_failed_mock.assert_awaited_once()
    call_args = mark_failed_mock.await_args.args
    assert call_args[1] == scan_id
    assert "connection" in call_args[2] or "lost" in call_args[2]


async def test_db_outage_during_save_scan_result_marks_scan_failed(monkeypatch):
    """The final `save_scan_result` call (the "data_structuring" stage) now has its
    own try/except that calls mark_scan_failed before re-raising, mirroring the
    website_scan/classification stages -- a DB outage here no longer leaves the scan
    row stuck at status="running" forever."""
    scan_id = uuid.uuid4()
    outage = OperationalError(
        "INSERT INTO website_pages ...", {}, Exception("server closed the connection unexpectedly")
    )

    mark_failed_mock = AsyncMock()
    _patch_common(monkeypatch)
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_failed", mark_failed_mock)
    monkeypatch.setattr(scan_service.scan_repository, "save_scan_result", AsyncMock(side_effect=outage))

    with pytest.raises(OperationalError):
        await scan_service.execute_scan_and_persist(scan_id, "https://example.com/")

    # Desired contract, matching the one the crawl-stage test above already confirms
    # holds for earlier stages: a DB outage anywhere in the pipeline should leave the
    # scan row "failed" (with `error` populated), never stuck at "running"/"pending".
    mark_failed_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Partial crawl failure
# ---------------------------------------------------------------------------


class _FakePage:
    """Stands in for a Playwright `Page`. Only the handful of methods
    `_crawl_full_site` actually calls are implemented."""

    def __init__(self, *, html: str | None = None, goto_error: Exception | None = None, http_status: int = 200):
        self._html = html
        self._goto_error = goto_error
        self._http_status = http_status
        self.closed = False

    def on(self, _event, _handler):
        pass

    async def goto(self, _url, timeout=None, wait_until=None):
        if self._goto_error is not None:
            raise self._goto_error
        return MagicMock(status=self._http_status)

    async def content(self):
        return self._html

    async def evaluate(self, _script, _candidates=None):
        # Real Page.evaluate() supports a single positional arg (the CMP global-var
        # probe passes a second arg; the scroll-step and shadow-DOM-text scripts
        # don't) -- _candidates defaults to None so all three call shapes work
        # against this fake. "" is falsy like [] and False, so it's a safe stand-in
        # for every current caller: the global-var filter (wrapped in set(...)), the
        # scroll "reached bottom" check, and the shadow-DOM text collector (which
        # falls back to parsed.visible_text when falsy) all treat it as "nothing".
        return ""

    async def wait_for_timeout(self, _ms):
        pass

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    @property
    def frames(self):
        # No iframes in this fixture's fake pages -- frames == [main_frame] means
        # _collect_iframe_signal_evidence's frame loop is a correct, real no-op here.
        return [self]

    @property
    def main_frame(self):
        return self


class _FakeBrowserContext:
    """Stands in for a Playwright `BrowserContext`. Pages are handed out in the exact
    order `_crawl_full_site`'s BFS loop will request them (root page first, then
    whatever it discovers via links), so the fixture below controls which URL each
    `_FakePage` corresponds to."""

    def __init__(self, pages: list[_FakePage]):
        self._pages = deque(pages)

    async def new_page(self):
        return self._pages.popleft()

    async def cookies(self):
        return []


_ROOT_HTML = """
<html><head><title>Home</title></head><body>
  <a href="/about">About</a>
  <form id="signup"><input type="email" name="email" required></form>
</body></html>
"""


async def test_partial_page_failure_is_skipped_and_does_not_discard_prior_evidence(monkeypatch):
    """One sub-page (`/about`) fails outright mid-crawl (goto() raises on every
    attempt, simulating a persistent connection reset). `_crawl_full_site`'s per-page
    handling records the failure explicitly and `continue`s rather than propagating --
    so this test confirms the *desired* "fail safely, never silently lose evidence,
    never silently drop the failed page either" contract: evidence already gathered
    from the root page (which succeeded) is still returned, AND the failed page gets
    its own explicit PageRecord (scanner-hardening audit, section 4) rather than being
    dropped from `pages` entirely.

    goto() raises the same error on every retry attempt (bounded at
    crawler._NAV_MAX_ATTEMPTS), so asyncio.sleep is patched to a no-op here purely to
    keep this unit test fast -- the real backoff delay itself is exercised for real in
    the live scanner-hardening validation, not here."""
    monkeypatch.setattr("app.scanner.crawler.asyncio.sleep", AsyncMock())

    root_url = "https://example.com/"
    about_url = "https://example.com/about"

    root_page = _FakePage(html=_ROOT_HTML, http_status=200)
    about_page = _FakePage(goto_error=Exception(f"net::ERR_CONNECTION_RESET at {about_url}"))
    context = _FakeBrowserContext([root_page, about_page])

    settings = get_settings()

    # Must not raise: a single failed sub-page should never abort the whole crawl.
    pages, forms, _cookies, _trackers, _policies, _consent_signal, diagnostics = await _crawl_full_site(
        context, root_url, settings, disallowed_prefixes=set()
    )

    # The page that failed to load (after exhausting bounded retries) still gets an
    # explicit PageRecord -- never silently dropped from `pages` -- distinguishable
    # from a real success via discovered_via + a null http_status.
    assert [p.url for p in pages] == [root_url, about_url]
    about_record = pages[1]
    assert about_record.discovered_via == "failed"
    assert about_record.http_status is None
    assert diagnostics["pages_failed"] == 1
    assert diagnostics["pages_timeout"] == 0

    # Every attempted page was still visited (both fake pages were consumed) and
    # cleaned up rather than leaked.
    assert len(context._pages) == 0
    assert about_page.closed is True

    # Evidence already gathered from the page that DID succeed is kept, not discarded
    # just because a later page in the same crawl failed.
    assert len(forms) == 1
    assert forms[0].purpose_guess == "newsletter_signup"
