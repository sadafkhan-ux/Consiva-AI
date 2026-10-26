"""What a scan that could not load the site (an HTTP/2 protocol error, a 30 s timeout) produces, and what a score built on little evidence says.

Written against two historical failures (www.nykaa.com: net::ERR_HTTP2_PROTOCOL_ERROR; www.tradeuno.com: Timeout 30000ms exceeded) using scripted pages, not the live sites.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.api.v1.routes import consent_scans as route
from app.config import get_settings
from app.core.exceptions import ScanNotReadyError
from app.scanner import crawler
from app.scanner.crawler import _crawl_full_site
from app.scanner.failures import ScanNotPossible
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import TODAY, evidence, tr
from tests.test_navigation_resilience import (  # noqa: F401
    ROOT,
    Clock,
    ScriptedPage,
    SiteContext,
    clock,
    quick,
)

NYKAA = "https://www.nykaa.com/"
TRADEUNO = "https://www.tradeuno.com/"


class TestAnHttp2ProtocolErrorOnTheEnteredAddress:
    H2 = PlaywrightError(f"net::ERR_HTTP2_PROTOCOL_ERROR at {NYKAA}")

    async def test_it_is_a_short_bounded_attempt_not_a_minutes_long_one(self, clock):  # noqa: F811
        page = ScriptedPage([self.H2] * 50)
        _, status, exc, attempts = await crawler._navigate_with_retry(page, NYKAA, 30000, window_s=120)
        assert status == "failed" and exc is self.H2
        assert sum(clock.slept) <= crawler._NAV_REFUSED_WINDOW_S + 1, "a refusing site is retried for the short window only"
        assert attempts == page.calls < crawler._NAV_MAX_ATTEMPTS

    async def test_the_scan_ends_as_not_possible_with_the_reason_and_no_pages(self, quick):  # noqa: F811
        ctx = SiteContext({NYKAA: ("error", self.H2), "*": ("error", self.H2)})
        with pytest.raises(ScanNotPossible) as caught:
            await _crawl_full_site(ctx, NYKAA, get_settings(), disallowed_prefixes=set())
        text = str(caught.value)
        assert caught.value.code == "refused" and caught.value.retryable is False
        assert "Could not reach the site at all" in text and "ERR_HTTP2_PROTOCOL_ERROR" in text and "Nothing was assessed" in text

    async def test_the_scan_is_not_retried_by_the_queue(self):
        from app.scanner.failures import explain_seed_failure
        err = explain_seed_failure(NYKAA, self.H2, 4)
        assert getattr(err, "retryable", True) is False


class TestATimeoutOnTheEnteredAddress:
    def test_the_per_page_timeout_is_still_30_seconds_and_the_seed_window_bounds_the_retries(self):
        s = get_settings()
        assert s.scanner_timeout_seconds == 30
        assert s.scanner_nav_retry_seconds_seed == 120 and s.scanner_nav_retry_seconds_page == 45

    async def test_retries_stop_inside_the_window_and_the_scan_is_a_retryable_failure_with_no_pages(self, quick):  # noqa: F811
        timeout = PlaywrightTimeoutError("Timeout 30000ms exceeded")
        ctx = SiteContext({TRADEUNO: ("error", timeout), "*": ("error", timeout)})
        with pytest.raises(RuntimeError) as caught:
            await _crawl_full_site(ctx, TRADEUNO, get_settings(), disallowed_prefixes=set())
        assert not isinstance(caught.value, ScanNotPossible), "a slow site may answer later: the queue may try again, with growing waits"
        assert "Could not reach the site at all" in str(caught.value) and "too slow or unavailable" in str(caught.value)

    async def test_the_waits_are_bounded_by_the_seed_window(self, clock):  # noqa: F811
        page = ScriptedPage([PlaywrightTimeoutError("Timeout 30000ms exceeded")] * 100)
        await crawler._navigate_with_retry(page, TRADEUNO, 30000, window_s=120)
        assert sum(clock.slept) <= 120 and page.calls <= crawler._NAV_MAX_ATTEMPTS


class TestAFailedScanHasNoReportAndNoScore:
    def _scan(self, status):
        stamp = None
        return SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4(), website_id=uuid.uuid4(), url=NYKAA, status=status, created_at=stamp, started_at=stamp, completed_at=stamp)

    @pytest.mark.parametrize("status", ["failed", "queued", "running"])
    async def test_the_pdf_report_of_a_scan_that_did_not_complete_is_refused(self, monkeypatch, status):
        scan = self._scan(status)
        monkeypatch.setattr(route.scan_repository, "get_scan", AsyncMock(return_value=scan))
        monkeypatch.setattr(route.scan_repository, "get_website_by_id", AsyncMock(return_value=SimpleNamespace(org_id=scan.org_id)))
        user = SimpleNamespace(org_id=str(scan.org_id), user_id=str(uuid.uuid4()), role="member")
        with pytest.raises(ScanNotReadyError, match="has no report"):
            await route.get_scan_report_pdf(scan.id, MagicMock(headers={}), variant="long", user=user, db=AsyncMock())

    def test_evidence_with_no_scanned_page_gives_no_score_and_zero_coverage(self):
        block = assess(evidence(pages=0), TODAY).readiness
        assert block.status == "insufficient_evidence" and block.score_percent is None
        assert block.coverage_percent == 0.0 and block.points_assessed == 0


class TestTheScreenshotsZeroPercent:
    """Two requirements Not Met, the untestable ones Unable to Verify, entity-specific ones Not Applicable: 0% is what the formula gives, and it says how little it rests on."""

    def test_zero_percent_is_the_formula_with_unable_to_verify_outside_the_denominator(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none", policies=()), TODAY)
        b = s.readiness
        assert b.status == "scored" and b.score_percent == 0.0
        assert b.points_earned == 0 and b.points_assessed == 9, "only the two assessed (Not Met) requirements are in the denominator"
        assert b.unable_to_verify_weight == 14 and b.applicable_weight == 23, "the untested weight is reported, not scored"
        assert b.coverage_percent == 39.1 and b.caution and "caution" in b.caution

    def test_more_unable_to_verify_weight_never_changes_what_was_earned_it_can_only_withhold_the_score(self):
        """Declaring more entity-specific duties adds Unable-to-Verify weight. The points earned and assessed do not move; coverage falls, and below the minimum the score is withheld
        rather than shown (missing evidence is never turned into a number)."""
        from app.scoring.dpdp_score import MIN_COVERAGE_PERCENT, Applicability
        ev = evidence(trackers=[tr(1)], mechanism="none", policies=())
        base = assess(ev, TODAY).readiness
        more = assess(ev, TODAY, Applicability(True, True, True, True)).readiness
        assert more.unable_to_verify_weight > base.unable_to_verify_weight and more.coverage_percent < base.coverage_percent
        assert (more.points_earned, more.points_assessed) == (base.points_earned, base.points_assessed)
        assert more.status == ("scored" if more.coverage_percent >= MIN_COVERAGE_PERCENT else "insufficient_evidence")
        assert more.score_percent in (None, 0.0)
