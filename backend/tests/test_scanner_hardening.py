"""Unit tests for the scanner-hardening audit's bounded-safety contracts:
navigation retry (bounded, backs off, preserves the original error) and progressive
scroll (bounded on both step count and wall-clock duration -- must terminate even
against a page that never reports "at bottom", the genuine infinite-scroll case).

Real-website verification for the actual detection improvements (more trackers found
after scrolling, a real OneTrust accept/reject round-trip, etc.) was done live against
swaransoft.com/nvidia.com/klaviyo.com/hubspot.com as part of this audit -- these tests
cover the bounded-safety properties that are impractical to reliably trigger on demand
against a real site (an infinite-scroll page, a persistently failing navigation).
"""

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.scanner import crawler


class _FakeNavPage:
    """Stands in for a Playwright Page for _navigate_with_retry only."""

    def __init__(self, errors: list[Exception | None]):
        # One entry per goto() call; None means "succeed this attempt".
        self._errors = list(errors)
        self.goto_calls = 0

    async def goto(self, _url, timeout=None, wait_until=None):
        self.goto_calls += 1
        err = self._errors.pop(0)
        if err is not None:
            raise err
        return type("Response", (), {"status": 200})()


async def test_navigate_with_retry_succeeds_after_transient_failure(monkeypatch):
    monkeypatch.setattr(crawler.asyncio, "sleep", AsyncMock())
    page = _FakeNavPage([PlaywrightError("net::ERR_CONNECTION_RESET"), None])

    response, status, exc, attempts = await crawler._navigate_with_retry(page, "https://example.com/", 5000)

    assert status == "success"
    assert attempts == 2
    assert exc is None
    assert response.status == 200
    assert page.goto_calls == 2


async def test_navigate_with_retry_is_bounded_and_preserves_original_error(monkeypatch):
    """A PERMANENTLY failing navigation must not be retried forever -- exactly
    _NAV_MAX_ATTEMPTS attempts, then give up -- and the real underlying error must
    still be available to the caller, not replaced with a generic message."""
    monkeypatch.setattr(crawler.asyncio, "sleep", AsyncMock())
    real_error = PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://example.com/")
    page = _FakeNavPage([real_error] * crawler._NAV_MAX_ATTEMPTS)

    response, status, exc, attempts = await crawler._navigate_with_retry(page, "https://example.com/", 5000)

    assert status == "failed"
    assert attempts == crawler._NAV_MAX_ATTEMPTS
    assert page.goto_calls == crawler._NAV_MAX_ATTEMPTS
    assert response is None
    assert exc is real_error  # original error preserved, not swallowed/replaced


async def test_navigate_with_retry_distinguishes_timeout_from_other_failures(monkeypatch):
    monkeypatch.setattr(crawler.asyncio, "sleep", AsyncMock())
    page = _FakeNavPage([PlaywrightTimeoutError("Timeout 30000ms exceeded")] * crawler._NAV_MAX_ATTEMPTS)

    _, status, _, _ = await crawler._navigate_with_retry(page, "https://example.com/", 5000)

    assert status == "timeout"


class _FakeInfiniteScrollPage:
    """Simulates a genuine infinite-scroll page: scrollHeight grows without bound and
    "at bottom" is never reported, so the ONLY thing that can stop _progressive_scroll
    is its own step-count/duration bounds -- exactly the property under test."""

    def __init__(self):
        self.evaluate_calls = 0

    async def evaluate(self, _script):
        self.evaluate_calls += 1
        return False  # never "at bottom"

    async def wait_for_timeout(self, _ms):
        pass


async def test_progressive_scroll_terminates_on_step_bound_against_infinite_scroll_page():
    page = _FakeInfiniteScrollPage()

    steps = await crawler._progressive_scroll(page)

    assert steps == crawler._SCROLL_MAX_STEPS
    assert page.evaluate_calls == crawler._SCROLL_MAX_STEPS


async def test_progressive_scroll_stops_early_at_real_bottom():
    class _FakeShortPage:
        def __init__(self):
            self.calls = 0

        async def evaluate(self, _script):
            self.calls += 1
            return self.calls >= 2  # "at bottom" on the second check

        async def wait_for_timeout(self, _ms):
            pass

    page = _FakeShortPage()
    steps = await crawler._progressive_scroll(page)

    assert steps == 2
    assert steps < crawler._SCROLL_MAX_STEPS


async def test_progressive_scroll_survives_a_page_that_errors_on_evaluate():
    """A page that errors out mid-scroll (detached/closed) must not raise -- scrolling
    is best-effort evidence-gathering, never fatal to the surrounding page fetch."""

    class _FakeErroringPage:
        async def evaluate(self, _script):
            raise PlaywrightError("Execution context was destroyed")

        async def wait_for_timeout(self, _ms):
            pass

    steps = await crawler._progressive_scroll(_FakeErroringPage())
    assert steps == 0


@pytest.mark.parametrize("max_concurrent", [1, 4, 8])
def test_scanner_max_concurrent_pages_is_configurable(max_concurrent):
    """Section 9: concurrency must be a Settings field, not a hardcoded constant."""
    from app.config import Settings

    settings = Settings(
        nvidia_api_key="x", nvidia_llm_model="x", nvidia_embed_model="x",
        supabase_url="http://localhost", supabase_service_role_key="x",
        database_url="postgresql://localhost/x", supabase_jwt_secret="x", app_env="development",
        scanner_max_concurrent_pages=max_concurrent,
    )
    assert settings.scanner_max_concurrent_pages == max_concurrent


def test_scanner_max_concurrent_pages_default_is_unchanged():
    """The default must stay 4 (the previously hardcoded, live-measured value) --
    moving it into Settings must not silently change existing behavior."""
    from app.config import Settings

    settings = Settings(
        nvidia_api_key="x", nvidia_llm_model="x", nvidia_embed_model="x",
        supabase_url="http://localhost", supabase_service_role_key="x",
        database_url="postgresql://localhost/x", supabase_jwt_secret="x", app_env="development",
    )
    assert settings.scanner_max_concurrent_pages == 4


# ---------------------------------------------------------------------------
# Business-acceptance audit: three-state comparison (consent_states accumulation)
# and the honest interaction-status vocabulary (cmp_not_found / cmp_not_automatable
# / page_unreachable / click_failed / clicked).
# ---------------------------------------------------------------------------

from app.scanner.crawler import _final_interaction_status, _merge_cookie_pass, _merge_tracker_pass
from app.scanner.schemas import CookieRecord, TrackerRecord


def test_merge_tracker_pass_accumulates_consent_states_across_all_three_passes():
    """A tracker seen in all three real passes must end up as ONE record with all
    three states listed -- not three separate rows, and not losing any state."""
    pre = [TrackerRecord(local_id="tracker-0", script_src="https://analytics.example.com/collect?x=1",
                          consent_states=["pre_consent"])]
    accept = [TrackerRecord(local_id="tracker-0", script_src="https://analytics.example.com/collect?x=2",
                             consent_states=["post_accept"])]
    reject = [TrackerRecord(local_id="tracker-0", script_src="https://analytics.example.com/collect?x=3",
                             consent_states=["post_reject"])]

    merged = _merge_tracker_pass(pre, accept)
    merged = _merge_tracker_pass(merged, reject)

    assert len(merged) == 1  # same tracker (query-stripped dedup key), not three rows
    assert set(merged[0].consent_states) == {"pre_consent", "post_accept", "post_reject"}


def test_merge_tracker_pass_only_after_accept_stays_distinct_from_pre_consent():
    """A tracker that ONLY appears after Accept (never pre-consent, never post-reject)
    must be distinguishable from one that fires unconditionally -- this is category
    "appears only after Accept" from the three-state comparison requirement."""
    pre = [TrackerRecord(local_id="tracker-0", script_src="https://ads.example.com/pixel",
                          consent_states=["pre_consent"])]
    accept_only = [TrackerRecord(local_id="tracker-1", script_src="https://marketing.example.com/pixel",
                                  consent_states=["post_accept"])]

    merged = _merge_tracker_pass(pre, accept_only)

    by_src = {t.script_src: t.consent_states for t in merged}
    assert by_src["https://ads.example.com/pixel"] == ["pre_consent"]
    assert by_src["https://marketing.example.com/pixel"] == ["post_accept"]


def test_merge_cookie_pass_keys_on_name_and_domain_not_local_id():
    base = [CookieRecord(local_id="cookie-0", name="_ga", domain="example.com", consent_states=["pre_consent"])]
    new = [CookieRecord(local_id="cookie-0", name="_ga", domain="example.com", consent_states=["post_reject"])]

    merged = _merge_cookie_pass(base, new)

    assert len(merged) == 1
    assert set(merged[0].consent_states) == {"pre_consent", "post_reject"}  # still fires after reject


@pytest.mark.parametrize("raw_status,mechanism_type,expected", [
    ("clicked", "cmp", "clicked"),
    ("clicked", "none", "clicked"),
    ("click_failed", "cmp", "click_failed"),
    ("page_unreachable", "none", "page_unreachable"),
    ("not_found", "none", "cmp_not_found"),        # no mechanism was ever detected at all
    ("not_found", "banner", "cmp_not_automatable"),  # a real mechanism exists, just couldn't be driven
    ("not_found", "cmp", "cmp_not_automatable"),
])
def test_final_interaction_status_composition(raw_status, mechanism_type, expected):
    """Section 4/8's core distinction: PAGE_UNREACHABLE / CMP_NOT_FOUND /
    CMP_NOT_AUTOMATABLE must never be conflated. Confirmed live against a real site
    (klaviyo.com, pre-Transcend-fix): mechanism_type="none" + reject "not_found" ->
    "cmp_not_found"; after adding Transcend to the catalog, mechanism_type="cmp" +
    the SAME raw "not_found" (no reject-all control exists) correctly became
    "cmp_not_automatable" instead -- proving this composition is what changed the
    real, user-facing meaning of that result, not just its label."""
    assert _final_interaction_status(raw_status, mechanism_type) == expected
