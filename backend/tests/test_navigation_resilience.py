"""Navigation errors, retries and failed scans.

Found by scanning twenty large real sites (amazon.in, flipkart.com, nytimes.com, hdfcbank.com, irctc.co.in, tcs.com, zomato.com ...):

  * every error was retried exactly three times with waits of 1 s and 2 s, so a certificate error cost three attempts and a short outage ended the scan;
  * an HTTP error status was treated as a page: tcs.com answered "403 Access Denied", the scan "completed", and the report scored the error page (55.6%,
    "no privacy policy found");
  * pages that failed, timed out or redirected away used up the page budget, so a scan limited to 5 pages could end having scanned 1 (hdfcbank.com);
  * a crawl that scanned nothing (npci.org.in: its robots.txt disallows every crawler but named search engines) was reported as completed;
  * after a navigation that broke (zomato.com), page.close() never returned and the scan sat until the whole-scan ceiling killed it.
"""

import asyncio
import contextlib
from collections import deque
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import get_settings
from app.scanner import crawler
from app.scanner.crawler import _crawl_full_site
from app.scanner.failures import (
    DNS,
    PERMANENT,
    REFUSED,
    TRANSIENT,
    NavigationHttpError,
    ScanNotPossible,
    classify_navigation_failure,
    explain_seed_failure,
    parse_retry_after,
)


class Clock:
    """A simulated clock: sleeping advances it, nothing really waits."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(crawler.time, "monotonic", c.monotonic)
    monkeypatch.setattr(crawler.asyncio, "sleep", c.sleep)
    return c


class ScriptedPage:
    """goto() answers from a script: an exception to raise, or an int HTTP status (with optional headers)."""

    def __init__(self, script):
        self.script = deque(script)
        self.calls = 0

    async def goto(self, _url, timeout=None, wait_until=None):
        self.calls += 1
        step = self.script[0] if len(self.script) == 1 else self.script.popleft()
        if isinstance(step, BaseException):
            raise step
        status, headers = step if isinstance(step, tuple) else (step, {})
        return type("Response", (), {"status": status, "headers": headers})()


TIMEOUT = lambda: PlaywrightTimeoutError("Timeout 30000ms exceeded")
RESET = lambda: PlaywrightError("net::ERR_CONNECTION_RESET at https://x.test/")
H2 = lambda: PlaywrightError("net::ERR_HTTP2_PROTOCOL_ERROR at https://x.test/")
CERT = lambda: PlaywrightError("net::ERR_CERT_AUTHORITY_INVALID at https://x.test/")


# ── what kind of failure is it ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestClassification:
    @pytest.mark.parametrize("error,kind", [
        (PlaywrightTimeoutError("Timeout 30000ms exceeded"), TRANSIENT),
        (PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://x.test/"), DNS),
        (PlaywrightError("net::ERR_NETWORK_CHANGED"), TRANSIENT),
        (PlaywrightError("net::ERR_TIMED_OUT"), TRANSIENT),
        (PlaywrightError("net::ERR_HTTP2_PROTOCOL_ERROR at https://x.test/"), REFUSED),
        (PlaywrightError("net::ERR_EMPTY_RESPONSE"), REFUSED),
        (PlaywrightError("net::ERR_CONNECTION_RESET"), REFUSED),
        (PlaywrightError("net::ERR_CONNECTION_REFUSED"), REFUSED),
        (PlaywrightError("net::ERR_CERT_DATE_INVALID"), PERMANENT),
        (PlaywrightError("net::ERR_CERT_AUTHORITY_INVALID"), PERMANENT),
        (PlaywrightError("net::ERR_INVALID_URL"), PERMANENT),
        (PlaywrightError("net::ERR_UNSAFE_PORT"), PERMANENT),
        (PlaywrightError("net::ERR_BLOCKED_BY_CLIENT"), PERMANENT),
        (PlaywrightError("net::ERR_ABORTED"), PERMANENT),
        (NavigationHttpError(503, "u"), TRANSIENT),
        (NavigationHttpError(429, "u"), TRANSIENT),
        (NavigationHttpError(502, "u"), TRANSIENT),
        (NavigationHttpError(403, "u"), PERMANENT),
        (NavigationHttpError(404, "u"), PERMANENT),
        (NavigationHttpError(401, "u"), PERMANENT),
        (None, TRANSIENT),
    ])
    def test_the_kind(self, error, kind):
        assert classify_navigation_failure(error) == kind

    def test_retry_after_is_read_and_bounded(self):
        assert parse_retry_after("7") == 7.0
        assert parse_retry_after("9999") == 30.0           # a site cannot park a scan
        assert parse_retry_after(None) is None

    NOW = datetime(2026, 10, 21, 7, 27, 50, tzinfo=UTC)

    def test_an_http_date_in_the_future_is_the_wait_until_then(self):
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=self.NOW) == 10.0
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 +0000", now=self.NOW) == 10.0

    def test_an_http_date_far_away_is_bounded_like_seconds(self):
        assert parse_retry_after("Wed, 21 Oct 2026 09:00:00 GMT", now=self.NOW) == 30.0
        assert parse_retry_after("Wed, 21 Oct 2026 09:00:00 GMT", ceiling=5, now=self.NOW) == 5.0

    @pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:27:00 GMT", "Wed, 21 Oct 2026 07:27:50 GMT", "Wed, 01 Jan 2020 00:00:00 GMT"])
    def test_a_past_date_means_use_the_normal_backoff(self, value):
        assert parse_retry_after(value, now=self.NOW) is None

    @pytest.mark.parametrize("value", ["soon", "", "   ", "-5", "1.5", "Wed, 99 Foo 2026 99:99:99 GMT", "0x10", "7 seconds"])
    def test_malformed_values_are_ignored_not_guessed(self, value):
        assert parse_retry_after(value, now=self.NOW) is None

    def test_a_date_without_a_zone_is_read_as_utc(self):
        assert parse_retry_after("21 Oct 2026 07:28:00", now=self.NOW) == 10.0


# ── retries fit the failure and are bounded by time ──────────────────────────────────────────────────────────────────────────────────────────────

class TestRetries:
    async def test_a_permanent_error_is_not_retried_at_all(self, clock):
        page = ScriptedPage([CERT()])
        _, status, exc, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000)
        assert (status, attempts, page.calls, clock.slept) == ("failed", 1, 1, [])
        assert "ERR_CERT" in str(exc)

    async def test_an_http_error_page_is_a_failed_navigation_not_a_page(self, clock):
        for code in (403, 404, 410, 451):
            page = ScriptedPage([code])
            response, status, exc, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000)
            assert response is None and status == "failed" and attempts == 1, code
            assert isinstance(exc, NavigationHttpError) and exc.status == code

    async def test_a_site_that_says_try_again_is_retried_until_it_does(self, clock):
        page = ScriptedPage([503, 502, 200])
        response, status, _exc, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000)
        assert status == "success" and attempts == 3 and response.status == 200

    async def test_retry_after_is_honoured(self, clock):
        page = ScriptedPage([(429, {"retry-after": "7"}), 200])
        _, status, _, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000)
        assert status == "success" and attempts == 2 and clock.slept == [7.0]

    async def test_a_transient_failure_gets_the_whole_window_not_three_tries(self, clock):
        """Timeouts for a whole minute: the old rule gave up after three attempts and 3 s of waiting; the window keeps trying with doubling waits."""
        page = ScriptedPage([TIMEOUT()])
        _, status, _, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000, window_s=120)
        waited = sum(clock.slept)
        assert status == "timeout"
        assert attempts >= 7, attempts                          # 1, 2, 4, 8, 16, 30 ... s apart
        assert waited < 120 and attempts <= crawler._NAV_MAX_ATTEMPTS
        gaps = clock.slept
        assert gaps[:5] == sorted(gaps[:5])                      # the waits double: about 1, 2, 4, 8, 16 s (a little jitter on each) ...
        assert max(gaps) <= crawler._NAV_BACKOFF_MAX_S * 1.26    # ... then stop growing at the ceiling

    async def test_a_refusal_is_retried_only_briefly(self, clock):
        page = ScriptedPage([H2()])
        _, status, _, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000, window_s=120)
        assert status == "failed" and 3 <= attempts <= 5, attempts
        assert sum(clock.slept) < crawler._NAV_REFUSED_WINDOW_S    # not the 120-second window: a site that keeps resetting is turning the scanner away

    async def test_a_blip_that_clears_is_ridden_out(self, clock):
        page = ScriptedPage([RESET(), RESET(), 200])
        _, status, _, attempts = await crawler._navigate_with_retry(page, "https://x.test/", 5000)
        assert status == "success" and attempts == 3

    async def test_the_window_is_shorter_for_a_secondary_page_than_for_the_seed(self, clock):
        seed = ScriptedPage([TIMEOUT()])
        await crawler._navigate_with_retry(seed, "https://x.test/", 5000, window_s=120)
        seed_attempts = seed.calls
        other = ScriptedPage([TIMEOUT()])
        await crawler._navigate_with_retry(other, "https://x.test/p", 5000, window_s=45)
        assert other.calls < seed_attempts

    async def test_a_name_that_does_not_resolve_gives_up_in_the_short_window_not_the_full_one(self, clock):
        dns = PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://nosuchsite.example/")
        dead, slow = ScriptedPage([dns] * 50), ScriptedPage([TIMEOUT()] * 50)
        await crawler._navigate_with_retry(dead, "https://nosuchsite.example/", 5000, window_s=120)
        waited_for_dns = sum(clock.slept)
        await crawler._navigate_with_retry(slow, "https://slow.example/", 5000, window_s=120)
        assert dead.calls < slow.calls
        assert waited_for_dns <= crawler._NAV_REFUSED_WINDOW_S + 1

    async def test_a_name_that_does_not_resolve_is_reported_as_that_and_not_retried_by_the_queue(self):
        err = explain_seed_failure("https://nosuchsite.example/", PlaywrightError("net::ERR_NAME_NOT_RESOLVED"), 4)
        assert isinstance(err, ScanNotPossible) and err.code == "dns_failure"
        assert "did not resolve" in str(err) and "Nothing was assessed" in str(err)

    async def test_the_original_error_is_kept(self, clock):
        err = PlaywrightError("net::ERR_NAME_NOT_RESOLVED at https://x.test/")
        _, _, exc, _ = await crawler._navigate_with_retry(ScriptedPage([err]), "https://x.test/", 5000, window_s=10)
        assert exc is err


# ── what a failed seed says, and whether trying again can help ───────────────────────────────────────────────────────────────────────────────────

class TestSeedFailureMessages:
    def test_access_denied_is_not_retryable_and_says_so_in_plain_words(self):
        e = explain_seed_failure("https://www.tcs.com", NavigationHttpError(403, "https://www.tcs.com"), 1)
        assert isinstance(e, ScanNotPossible) and e.retryable is False and e.code == "http_blocked"
        assert "HTTP 403" in str(e) and "no report was made" in str(e) and "refusing the scanner" in str(e)

    def test_a_missing_page_is_not_retryable(self):
        e = explain_seed_failure("https://x.test/", NavigationHttpError(404, "https://x.test/"), 1)
        assert isinstance(e, ScanNotPossible) and e.code == "http_error"

    def test_a_bad_certificate_is_not_retryable(self):
        e = explain_seed_failure("https://x.test/", CERT(), 1)
        assert isinstance(e, ScanNotPossible) and e.code == "unreachable" and "ERR_CERT_AUTHORITY_INVALID" in str(e)

    def test_a_site_that_keeps_resetting_the_connection_is_not_retryable(self):
        e = explain_seed_failure("https://www.irctc.co.in", H2(), 4)
        assert isinstance(e, ScanNotPossible) and e.code == "refused" and "4 attempts" in str(e) and "automated visitors" in str(e)

    def test_a_slow_site_is_retryable_later(self):
        e = explain_seed_failure("https://x.test/", TIMEOUT(), 8)
        assert not isinstance(e, ScanNotPossible) and isinstance(e, RuntimeError) and getattr(e, "retryable", True) is True
        assert "tried again later" in str(e)

    def test_a_site_that_stays_unavailable_is_retryable_later(self):
        e = explain_seed_failure("https://x.test/", NavigationHttpError(503, "https://x.test/"), 6)
        assert not isinstance(e, ScanNotPossible) and "HTTP 503" in str(e)

    def test_every_message_keeps_the_established_opening(self):
        for cause in (CERT(), H2(), TIMEOUT(), NavigationHttpError(403, "u"), NavigationHttpError(503, "u")):
            assert str(explain_seed_failure("https://x.test/", cause, 2)).startswith("Could not reach the site at all")

    def test_the_call_log_is_not_shown_to_a_reader(self):
        noisy = PlaywrightError('net::ERR_CERT_COMMON_NAME_INVALID at https://x.test/\nCall log:\n  - navigating to "https://x.test/", waiting until "domcontentloaded"')
        assert "Call log" not in str(explain_seed_failure("https://x.test/", noisy, 1))


# ── the crawl ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class SitePage:
    """A fake page that behaves as the SITE says for whichever address it is sent to."""

    def __init__(self, site, log):
        self.site, self.log, self.url, self.closed = site, log, None, False

    def on(self, *_a):
        pass

    async def goto(self, url, timeout=None, wait_until=None):
        self.url = url
        self.log.append(url)
        behaviour = self.site.get(url, self.site.get("*", ("ok", 200, "<html><head><title>p</title></head><body>ok</body></html>")))
        if behaviour[0] == "error":
            raise behaviour[1]
        self.html = behaviour[2] if len(behaviour) > 2 else "<html><head><title>x</title></head><body></body></html>"
        return MagicMock(status=behaviour[1], headers={})

    async def content(self):
        return getattr(self, "html", "")

    async def evaluate(self, *_a):
        return ""

    async def wait_for_load_state(self, *_a, **_k):
        return None

    async def wait_for_timeout(self, _ms):
        pass

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    @property
    def frames(self):
        return [self]

    @property
    def main_frame(self):
        return self


class SiteContext:
    def __init__(self, site):
        self.site, self.visited = site, []

    async def new_page(self):
        return SitePage(self.site, self.visited)

    async def cookies(self):
        return []


def links(*paths):
    return "<html><head><title>Home</title></head><body>" + "".join(f'<a href="{p}">{p}</a>' for p in paths) + "</body></html>"


ROOT = "https://x.test/"


def site_with(extra: dict):
    return {ROOT: ("ok", 200, links("/a", "/b", "/c", "/d", "/e", "/f", "/g", "/h", "/i")), **extra}


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(crawler.asyncio, "sleep", AsyncMock())


class TestTheCrawl:
    async def test_failed_pages_no_longer_use_up_the_page_budget(self, quick):
        """hdfcbank.com scanned 1 page of 5 because three timeouts spent the budget."""
        # The subject is the page BUDGET, not the slow-site early stop: with exactly three timeouts (the stop's default limit) whether it fires would depend on the order concurrent pages finish.
        s = get_settings().model_copy(update={"consent_web_max_pages": 5, "scanner_max_pages": 5, "scanner_stop_after_consecutive_timeouts": 0})
        site = site_with({f"https://x.test/{p}": ("error", PlaywrightTimeoutError("Timeout 30000ms exceeded")) for p in "abc"})
        pages, *_rest, diagnostics = await _crawl_full_site(SiteContext(site), ROOT, s, disallowed_prefixes=set())
        assert diagnostics["pages_scanned"] == 5 and diagnostics["pages_timeout"] == 3, {k: diagnostics[k] for k in ("pages_scanned", "pages_timeout", "pages_attempted", "stop_reason")}
        assert diagnostics["stop_reason"] == "page_limit"
        assert sum(1 for p in pages if p.discovered_via not in ("failed", "timeout")) == 5

    async def test_a_site_whose_pages_mostly_fail_cannot_keep_the_crawl_going_for_ever(self, quick):
        s = get_settings()
        site = {ROOT: ("ok", 200, links(*[f"/p{i}" for i in range(30)])), "*": ("error", PlaywrightError("net::ERR_CERT_AUTHORITY_INVALID"))}   # fails at once, so not a slow site
        site[ROOT] = ("ok", 200, links(*[f"/p{i}" for i in range(30)]))
        ctx = SiteContext(site)
        _pages, *_rest, diagnostics = await _crawl_full_site(ctx, ROOT, s, disallowed_prefixes=set())
        budget = diagnostics["page_limit"]
        assert diagnostics["pages_attempted"] <= budget + max(crawler._EXTRA_ATTEMPTS_MIN, budget)
        assert diagnostics["pages_scanned"] == 1 and diagnostics["stop_reason"] == "attempt_limit"

    async def test_an_access_denied_seed_fails_the_scan_instead_of_being_scanned(self, quick):
        """tcs.com, infosys.com and india.gov.in answer the scanner with HTTP 403: the scan used to complete and score the error page."""
        ctx = SiteContext({ROOT: ("ok", 403, "<html><head><title>Access Denied</title></head><body>Access Denied</body></html>")})
        with pytest.raises(ScanNotPossible) as caught:
            await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set())
        assert caught.value.code == "http_blocked" and caught.value.retryable is False
        assert str(caught.value).startswith("Could not reach the site at all")
        assert ctx.visited == [ROOT], "a 403 is not retried"

    async def test_a_seed_that_is_briefly_unavailable_is_retried_and_then_scanned(self, quick):
        calls = {"n": 0}

        class Flaky(SiteContext):
            async def new_page(self):
                page = await super().new_page()
                real = page.goto

                async def goto(url, timeout=None, wait_until=None):
                    calls["n"] += 1
                    if calls["n"] <= 2:
                        return MagicMock(status=503, headers={})
                    return await real(url, timeout=timeout, wait_until=wait_until)
                page.goto = goto
                return page

        pages, *_rest, diagnostics = await _crawl_full_site(Flaky({ROOT: ("ok", 200, links())}), ROOT, get_settings(), disallowed_prefixes=set())
        assert diagnostics["pages_scanned"] == 1 and calls["n"] == 3 and pages[0].http_status == 200

    async def test_a_seed_that_stays_unavailable_is_a_retryable_failure_with_the_real_reason(self, quick):
        ctx = SiteContext({ROOT: ("ok", 503, "")})
        with pytest.raises(RuntimeError) as caught:
            await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set())
        assert not isinstance(caught.value, ScanNotPossible) and "HTTP 503" in str(caught.value)

    async def test_an_error_page_among_the_other_pages_is_a_failed_page_not_a_scanned_one(self, quick):
        site = {ROOT: ("ok", 200, links("/gone", "/fine")), "https://x.test/gone": ("ok", 404, "<html><head><title>Not found</title></head></html>")}
        pages, *_rest, diagnostics = await _crawl_full_site(SiteContext(site), ROOT, get_settings(), disallowed_prefixes=set())
        gone = next(p for p in pages if p.url.endswith("/gone"))
        assert gone.discovered_via == "failed" and gone.http_status == 404
        assert diagnostics["pages_failed"] == 1 and diagnostics["pages_scanned"] == 2

    async def test_a_crawl_that_scanned_nothing_because_of_robots_txt_says_so(self, quick):
        """npci.org.in: robots.txt disallows every crawler but named search engines, the scan 'completed' with no pages and no reason."""
        ctx = SiteContext(site_with({}))
        with pytest.raises(ScanNotPossible) as caught:
            await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes={"/"})
        assert caught.value.code == "robots_disallowed" and "robots.txt" in str(caught.value) and ctx.visited == []

    async def test_a_cancelled_scan_with_nothing_scanned_is_still_the_partial_result_not_an_error(self, quick):
        async def stop():
            return True
        result = await _crawl_full_site(SiteContext(site_with({})), ROOT, get_settings(), disallowed_prefixes=set(), should_stop=stop)
        assert result[-1]["stop_reason"] == "cancelled"


class TestThePagesChosenDoNotDependOnTiming:
    """A slow homepage let the fastest pages (dead sitemap entries) use the attempt budget before the homepage's priority links, the privacy policy first among them,
    were known: 10 of 100 scans of one kind of site skipped the privacy page when the pages were heavy."""

    async def _run(self, seed_delay):
        events: list[tuple[str, str]] = []

        class TimedPage(SitePage):
            async def goto(self, url, timeout=None, wait_until=None):
                events.append(("start", url))
                if url == ROOT and seed_delay:
                    # not asyncio.sleep: the quick fixture replaces it with a no-op, and a homepage with no delay would test nothing
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.Event().wait(), seed_delay)
                result = await super().goto(url, timeout=timeout, wait_until=wait_until)
                events.append(("end", url))
                return result

        class TimedContext(SiteContext):
            async def new_page(self):
                return TimedPage(self.site, self.visited)

        dead = [f"https://x.test/page/{i}" for i in range(1, 25)]
        site = {ROOT: ("ok", 200, links("/privacy-policy", "/page/1")), "https://x.test/privacy-policy": ("ok", 200, "<html><head><title>Privacy</title></head><body>policy</body></html>"),
                **{u: ("ok", 404, "<html><head><title>no</title></head></html>") for u in dead}}
        ctx = TimedContext(site)
        pages, *_rest, diagnostics = await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set(), sitemap_urls=dead)
        return events, pages, diagnostics

    async def test_the_entered_address_is_loaded_alone_before_any_other_page_starts(self, quick):
        events, _pages, _d = await self._run(seed_delay=0.15)
        assert events[0] == ("start", ROOT)
        seed_end = events.index(("end", ROOT))
        assert all(kind == "end" or url == ROOT for kind, url in events[:seed_end]), "no other page may start before the seed has loaded"

    @pytest.mark.parametrize("seed_delay", [0.0, 0.05, 0.3])
    async def test_the_privacy_page_is_scanned_however_slowly_the_homepage_loads(self, quick, seed_delay):
        _events, pages, diagnostics = await self._run(seed_delay)
        scanned = {p.url for p in pages if p.discovered_via not in ("failed", "timeout")}
        assert "https://x.test/privacy-policy" in scanned, diagnostics

    async def test_the_same_pages_are_chosen_whatever_the_timing(self, quick):
        chosen = []
        for delay in (0.0, 0.1, 0.3):
            _events, pages, _d = await self._run(delay)
            chosen.append(sorted(p.url for p in pages))
        assert chosen[0] == chosen[1] == chosen[2]


class TestASiteThatOnlyTimesOut:
    """One slow site held a worker slot for 184 s while the median scan took 5 s, trying page after page that could not load."""

    TIMEOUT_SITE: ClassVar[dict] = {ROOT: ("ok", 200, links("/a", "/b", "/c", "/d", "/e", "/f", "/g", "/h", "/i")), "*": ("error", PlaywrightTimeoutError("Timeout 30000ms exceeded"))}

    async def test_the_crawl_stops_trying_and_completes_with_the_address_it_did_load(self, quick):
        s = get_settings().model_copy(update={"scanner_stop_after_consecutive_timeouts": 3})
        ctx = SiteContext(dict(self.TIMEOUT_SITE))
        pages, *_rest, diagnostics = await _crawl_full_site(ctx, ROOT, s, disallowed_prefixes=set())
        assert diagnostics["stop_reason"] == "slow_site"
        assert diagnostics["pages_scanned"] == 1 and diagnostics["pages_timeout"] >= 3
        assert diagnostics["pages_attempted"] <= 1 + 4, "only the pages already started finish; no new ones are begun"
        assert diagnostics["skipped_by_reason"].get("slow_site", 0) >= 1
        assert pages[0].url == ROOT and pages[0].http_status == 200

    async def test_pages_that_loaded_first_do_not_hide_a_site_that_then_only_times_out(self, quick):
        """What the load test found: the fast policy pages loaded, and every content page after them timed out."""
        s = get_settings().model_copy(update={"scanner_stop_after_consecutive_timeouts": 3})
        site = {ROOT: ("ok", 200, links("/privacy-policy", "/cookie-policy", *[f"/c{i}" for i in range(9)])),
                **{f"https://x.test/c{i}": ("error", PlaywrightTimeoutError("Timeout 30000ms exceeded")) for i in range(9)}}
        _pages, *_rest, diagnostics = await _crawl_full_site(SiteContext(site), ROOT, s, disallowed_prefixes=set())
        assert diagnostics["stop_reason"] == "slow_site"
        assert diagnostics["pages_scanned"] >= 2 and diagnostics["pages_timeout"] >= 3

    async def test_a_few_slow_pages_among_pages_that_load_never_cut_a_scan_short(self, quick):
        s = get_settings().model_copy(update={"scanner_stop_after_consecutive_timeouts": 3})
        site = site_with({"https://x.test/a": ("error", PlaywrightTimeoutError("Timeout 30000ms exceeded")), "https://x.test/b": ("error", PlaywrightTimeoutError("Timeout 30000ms exceeded"))})
        _pages, *_rest, diagnostics = await _crawl_full_site(SiteContext(site), ROOT, s, disallowed_prefixes=set())
        assert diagnostics["stop_reason"] == "page_limit" and diagnostics["pages_scanned"] == 5

    async def test_it_can_be_turned_off(self, quick):
        s = get_settings().model_copy(update={"scanner_stop_after_consecutive_timeouts": 0})
        _pages, *_rest, diagnostics = await _crawl_full_site(SiteContext(dict(self.TIMEOUT_SITE)), ROOT, s, disallowed_prefixes=set())
        assert diagnostics["stop_reason"] != "slow_site"
        assert diagnostics["pages_attempted"] > 5

    def test_the_report_has_words_for_it(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "report" / "scanPages.ts").read_text(encoding="utf-8")
        assert "slow_site:" in src and "too slow to scan more of it" in src


# ── resource handling ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestResources:
    async def test_closing_something_that_never_answers_does_not_hang_the_scan(self, monkeypatch):
        """zomato.com: page.close() never returned after a connection reset, and the scan sat until the whole-scan ceiling killed it."""
        monkeypatch.setattr(crawler, "_CLOSE_TIMEOUT_S", 0.05)

        class Stuck:
            async def close(self):
                await asyncio.sleep(3600)
        await asyncio.wait_for(crawler._close_quietly(Stuck(), "a page"), timeout=2)

    async def test_closing_never_raises(self):
        class Broken:
            async def close(self):
                raise PlaywrightError("Target closed")
        await crawler._close_quietly(Broken(), "a page")

    async def test_no_more_pages_load_at_once_than_the_process_allows_whichever_scans_they_belong_to(self, monkeypatch):
        monkeypatch.setattr(crawler, "_max_concurrent_navigations", lambda: 2)
        crawler._nav_slots.clear()
        live = {"now": 0, "peak": 0}

        class Slow:
            async def goto(self, *_a, **_k):
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
                await asyncio.sleep(0.03)
                live["now"] -= 1
                return type("R", (), {"status": 200, "headers": {}})()
        results = await asyncio.gather(*[crawler._navigate_with_retry(Slow(), f"https://x.test/{i}", 5000) for i in range(8)])
        assert all(r[1] == "success" for r in results)
        assert live["peak"] == 2
        crawler._nav_slots.clear()

    def test_the_limit_is_decided_from_the_machine_unless_set(self, monkeypatch):
        monkeypatch.setattr(crawler.os, "cpu_count", lambda: 8)
        assert crawler._max_concurrent_navigations() == 16
        monkeypatch.setattr(crawler.os, "cpu_count", lambda: 1)
        assert crawler._max_concurrent_navigations() == 6
        monkeypatch.setattr(crawler.os, "cpu_count", lambda: 64)
        assert crawler._max_concurrent_navigations() == 32
        monkeypatch.setattr(crawler, "get_settings", lambda: type("S", (), {"scanner_max_concurrent_navigations": 5})())
        assert crawler._max_concurrent_navigations() == 5

    def test_a_scan_that_cannot_succeed_keeps_that_across_the_windows_subprocess_path(self):
        import inspect
        src = inspect.getsource(crawler.run_scan_isolated)
        assert "ScanNotPossible: " in src and "raise ScanNotPossible" in src


class TestDeadPagesDoNotUseUpTheAttemptCeiling:
    """masterclass.gignaati.com: nine dead sitemap pages (HTTP 404) used the whole attempt ceiling and the scan covered 1 of its 5 pages."""

    def site(self, dead, good):
        home = ("ok", 200, links(*[f"/privacy-old-{i}" for i in range(dead)], *[f"/page-{i}" for i in range(good)]))
        site = {ROOT: home}
        for i in range(dead):
            site[f"https://x.test/privacy-old-{i}"] = ("ok", 404, "<html><head><title>Not found</title></head></html>")
        return site

    async def test_five_pages_are_still_scanned_behind_a_dozen_dead_ones(self, quick):
        ctx = SiteContext(self.site(dead=12, good=8))
        _pages, *_rest, d = await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set())
        assert d["pages_scanned"] == 5 and d["stop_reason"] == "page_limit", d["stop_reason"]
        assert d["pages_dead"] == 12 and d["pages_failed"] == 12, "dead pages are still recorded as failed, with their status"

    async def test_a_sitemap_of_hundreds_of_dead_pages_still_stops(self, quick):
        ctx = SiteContext(self.site(dead=200, good=8))
        _pages, *_rest, d = await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set())
        assert d["pages_dead"] <= 200 and d["pages_attempted"] <= 1 + crawler._DEAD_PAGE_ALLOWANCE + 10 + 2, d["pages_attempted"]
        assert d["stop_reason"] in ("attempt_limit", "page_limit")

    async def test_other_failures_still_use_the_ceiling(self, quick):
        site = {ROOT: ("ok", 200, links(*[f"/s-{i}" for i in range(12)], *[f"/g-{i}" for i in range(8)]))}
        for i in range(12):
            site[f"https://x.test/s-{i}"] = ("ok", 503, "")
        ctx = SiteContext(site)
        _pages, *_rest, d = await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=set())
        assert d.get("pages_dead", 0) == 0 and d["pages_attempted"] <= 11
