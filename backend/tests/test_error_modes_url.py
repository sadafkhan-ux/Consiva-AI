"""Error-mode coverage for URL/scan-request handling (POST /api/v1/consent/scans and
its downstream service/crawler code).

Three scenarios, matching the actual code paths (not assumed ones):

1. Malformed URL string ("not-a-url", ""): CreateScanRequest.url has a `@field_validator`
   requiring an http(s) scheme + netloc, so these ARE now rejected at the request-model
   layer with a clean 422. scan_service.request_scan also independently raises a typed
   `InvalidUrlError` (400) if `urlparse(url).netloc` is empty, for any caller that
   reaches the service layer directly without going through the Pydantic model.

2. Unreachable URL — two genuinely different failure points exist for this:
   (a) A domain that doesn't resolve at all (DNS NXDOMAIN) is caught *before* any
       Playwright navigation, by assert_safe_url's `socket.getaddrinfo` call inside
       request_scan's "url_validation" stage — this fails safely with a typed
       `ScanAuthorizationError` and the scan is marked failed.
   (b) A domain that resolves but whose page load then fails (connection refused,
       timeout, TLS error, etc.) reaches app/scanner/crawler.py's `_crawl_full_site`.
       For the root/seed URL specifically, a navigation failure now raises a
       `RuntimeError` (rather than being silently swallowed like a secondary page
       failure would be) — an otherwise-unreachable site produces a raised exception,
       which execute_scan_and_persist's existing except-block turns into a scan marked
       "failed" with a clear error, instead of a false "completed" with empty evidence.

3. Daily per-org scan rate limit in request_scan: `recent_count >= settings.
   scanner_max_scans_per_org_per_day` raises RateLimitExceededError *before* any
   website/scan row is created. Verified at both sides of the threshold.

All tests run at the unit level: DB repository calls and audit/queue side effects are
mocked (per tests/test_llm_client.py's monkeypatch-the-module-attribute convention);
no live server, no live Postgres. assert_safe_url's DNS lookups are real (same as
tests/test_url_safety.py) since they're just fast local `getaddrinfo` calls with no
side effects.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.api.v1.routes.consent_scans import CreateScanRequest
from app.config import get_settings
from app.core.exceptions import InvalidUrlError, RateLimitExceededError, ScanAuthorizationError
from app.scanner.crawler import _crawl_full_site
from app.services import scan_service

UNREACHABLE_DOMAIN_URL = "https://this-domain-should-not-exist-consiva-test-12345.com"


class _NullAsyncCM:
    """Stands in for `async_session_factory()` — yields a pre-built fake session and
    does nothing on exit, so request_scan's `async with async_session_factory() as
    db:` blocks never touch a real connection."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------------
# 1. Invalid URL format
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["not-a-url", ""])
def test_create_scan_request_model_rejects_malformed_url_shape(bad_url):
    """CreateScanRequest.url has a @field_validator requiring an http(s) scheme +
    netloc, so a malformed URL or the empty string is rejected with a ValidationError
    at the request-model layer -- never reaches the service layer at all."""
    with pytest.raises(ValidationError):
        CreateScanRequest(url=bad_url, authorized=True)


@pytest.mark.parametrize("bad_url", ["not-a-url", ""])
async def test_request_scan_raises_invalid_url_error_for_unparseable_url(bad_url, monkeypatch):
    """scan_service.request_scan's own independent check (for callers that reach it
    directly, bypassing the Pydantic model) raises a typed InvalidUrlError (400) for a
    URL with no parseable netloc, before any DB call (count_scans_since is never
    reached) -- confirmed via the mock below never being awaited."""
    count_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(scan_service.scan_repository, "count_scans_since", count_mock)

    db = AsyncMock()
    with pytest.raises(InvalidUrlError, match="Could not parse a domain"):
        await scan_service.request_scan(
            db, org_id=uuid.uuid4(), user_id=uuid.uuid4(), url=bad_url, authorized=True
        )
    count_mock.assert_not_awaited()


@pytest.mark.parametrize("bad_url", ["not-a-url", ""])
def test_create_scan_endpoint_returns_422_for_malformed_url(bad_url):
    """End-to-end confirmation that a malformed `url` submitted to
    POST /api/v1/consent/scans is now rejected with a clean 422 validation error by
    FastAPI's automatic request-body validation, never reaching request_scan/the DB.

    Uses TestClient(app, raise_server_exceptions=False) (not `with TestClient(app)`)
    so lifespan never runs (see tests/test_api_validation.py's docstring).
    """
    import os

    import jwt
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated"},
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    response = client.post(
        "/api/v1/consent/scans",
        json={"url": bad_url, "authorized": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------------
# 2a. Unreachable URL -- domain doesn't resolve at all (caught safely, pre-crawl)
# --------------------------------------------------------------------------------


async def test_request_scan_fails_safely_for_nonexistent_domain(monkeypatch):
    """A domain that plain doesn't exist (NXDOMAIN) is rejected inside request_scan's
    "url_validation" stage by assert_safe_url's real `socket.getaddrinfo` call --
    *before* the job is ever enqueued for the Playwright crawler. This is the
    good/safe path: a typed ScanAuthorizationError, and the scan row is marked failed
    with that error recorded on it."""
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    scan_id = uuid.uuid4()
    website = SimpleNamespace(id=uuid.uuid4())
    scan = SimpleNamespace(id=scan_id, url=UNREACHABLE_DOMAIN_URL, status="pending")

    monkeypatch.setattr(scan_service.scan_repository, "count_scans_since", AsyncMock(return_value=0))
    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", AsyncMock(return_value=website))
    monkeypatch.setattr(scan_service.scan_repository, "create_scan", AsyncMock(return_value=scan))
    monkeypatch.setattr(scan_service.audit_service, "record", AsyncMock())
    mark_failed_mock = AsyncMock()
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_failed", mark_failed_mock)

    fake_validation_db = AsyncMock()
    monkeypatch.setattr(scan_service, "async_session_factory", lambda: _NullAsyncCM(fake_validation_db))

    db = AsyncMock()

    with pytest.raises(ScanAuthorizationError, match="Could not resolve hostname"):
        await scan_service.request_scan(
            db, org_id=org_id, user_id=user_id, url=UNREACHABLE_DOMAIN_URL, authorized=True
        )

    mark_failed_mock.assert_awaited_once()
    called_db, called_scan_id, called_error = mark_failed_mock.await_args.args
    assert called_db is fake_validation_db
    assert called_scan_id == scan_id
    assert "Could not resolve hostname" in called_error


# --------------------------------------------------------------------------------
# 2b. Unreachable URL -- domain resolves but the page load itself fails
# --------------------------------------------------------------------------------


class _FailingPage:
    """Fake Playwright Page whose goto() always raises, mimicking a connection-level
    failure (refused/timeout/TLS) on a domain that resolved fine per DNS but isn't
    actually serving anything."""

    def __init__(self, error: Exception):
        self._error = error
        self._closed = False

    def on(self, event, handler):
        pass

    async def goto(self, url, timeout=None, wait_until=None):
        raise self._error

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True


class _FakeContext:
    def __init__(self, page_factory):
        self._page_factory = page_factory

    async def new_page(self):
        return self._page_factory()

    async def cookies(self):
        return []


async def test_crawl_full_site_raises_on_root_page_connection_failure():
    """When the root/seed URL is the only page queued (a totally unreachable site) and
    its navigation raises (connection refused / timeout -- the failure a resolvable-
    but-dead domain actually produces), `_crawl_full_site` now raises a RuntimeError
    instead of silently returning a "clean", entirely empty result indistinguishable
    from a real site that simply has no cookies/trackers/forms/policies. This
    RuntimeError propagates up through run_scan_isolated (the child-process exit-code
    path) into execute_scan_and_persist's existing except-block, which marks the scan
    "failed" with the error message rather than falsely "completed"."""
    connection_error = PlaywrightError(
        f"net::ERR_CONNECTION_REFUSED at {UNREACHABLE_DOMAIN_URL}/"
    )
    context = _FakeContext(lambda: _FailingPage(connection_error))
    settings = get_settings()

    with pytest.raises(RuntimeError, match="Could not reach the site at all"):
        await _crawl_full_site(context, UNREACHABLE_DOMAIN_URL, settings, disallowed_prefixes=set())


# --------------------------------------------------------------------------------
# 3. Daily per-org scan rate limit
# --------------------------------------------------------------------------------


async def test_request_scan_raises_rate_limit_error_at_threshold(monkeypatch):
    """`recent_count >= scanner_max_scans_per_org_per_day` raises RateLimitExceededError
    -- exercised here with recent_count exactly AT the configured limit. Confirms the
    limit is enforced *before* any website/scan row is created (get_or_create_website
    is never reached)."""
    settings = get_settings()
    monkeypatch.setattr(
        scan_service.scan_repository, "count_scans_since",
        AsyncMock(return_value=settings.scanner_max_scans_per_org_per_day),
    )
    website_mock = AsyncMock()
    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", website_mock)

    db = AsyncMock()
    with pytest.raises(RateLimitExceededError, match="Scan rate limit reached"):
        await scan_service.request_scan(
            db, org_id=uuid.uuid4(), user_id=uuid.uuid4(), url="https://example.com", authorized=True
        )

    website_mock.assert_not_awaited()


async def test_request_scan_allows_scan_just_under_the_limit(monkeypatch):
    """One below the threshold must NOT raise RateLimitExceededError -- pins down the
    `>=` boundary from the other side. get_or_create_website is swapped for a sentinel
    exception so the test can prove the rate-limit gate was passed (execution reached
    the next step) without mocking the rest of request_scan's DB/queue side effects."""

    class _PassedRateLimitGate(Exception):
        pass

    settings = get_settings()
    monkeypatch.setattr(
        scan_service.scan_repository, "count_scans_since",
        AsyncMock(return_value=settings.scanner_max_scans_per_org_per_day - 1),
    )

    async def _boom(*args, **kwargs):
        raise _PassedRateLimitGate()

    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", _boom)

    db = AsyncMock()
    with pytest.raises(_PassedRateLimitGate):
        await scan_service.request_scan(
            db, org_id=uuid.uuid4(), user_id=uuid.uuid4(), url="https://example.com", authorized=True
        )
