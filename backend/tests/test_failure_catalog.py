"""Every scan failure is named the same way, for every site: a stable code, who must act, whether retrying can help, and the next step. Nothing here touches the network."""

import pytest
from playwright.async_api import Error as PlaywrightError

from app.scanner.failure_catalog import (
    BLOCKED,
    CANCELLED,
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    FAILED,
    PARTIALLY_COMPLETED,
    QUEUED,
    RUNNING,
    FailureCategory,
    classify_exception,
    classify_message,
    describe,
    scan_outcome,
)
from app.scanner.failures import (
    RobotsUnreadable,
    ScanNotPossible,
    certificate_failure,
    explain_seed_failure,
)

C = FailureCategory


class TestEveryCategoryIsComplete:
    @pytest.mark.parametrize("category", list(C))
    def test_each_has_a_code_an_explanation_an_owner_and_a_next_step(self, category):
        info = describe(category, detail="x", url="https://x.test/")
        assert info.code == category.value and info.title and len(info.explanation) > 40 and len(info.next_action) > 40
        assert info.owner in ("website_owner", "scanner_operator", "none") and info.url == "https://x.test/"
        assert info.as_dict()["category"] == category.value

    def test_the_fourteen_categories_the_scanner_reports(self):
        assert {c.value for c in C} == {
            "ROBOTS_TXT_DISALLOWED", "ROBOTS_TXT_UNAVAILABLE", "TLS_CERTIFICATE_EXPIRED", "TLS_CERTIFICATE_INVALID", "DNS_RESOLUTION_FAILED", "CONNECTION_FAILED", "REQUEST_TIMEOUT",
            "HTTP_ACCESS_DENIED", "AUTHENTICATION_REQUIRED", "RATE_LIMITED", "SERVER_ERROR", "BROWSER_NAVIGATION_FAILED", "PARTIAL_SCAN", "UNKNOWN_SCAN_ERROR"}

    @pytest.mark.parametrize("category", [C.ROBOTS_TXT_DISALLOWED, C.TLS_CERTIFICATE_EXPIRED, C.TLS_CERTIFICATE_INVALID, C.HTTP_ACCESS_DENIED, C.AUTHENTICATION_REQUIRED])
    def test_permanent_failures_are_never_retryable_and_belong_to_the_website_owner(self, category):
        info = describe(category)
        assert info.retryable is False and info.transient is False and info.owner == "website_owner"

    @pytest.mark.parametrize("category", [C.ROBOTS_TXT_UNAVAILABLE, C.REQUEST_TIMEOUT, C.RATE_LIMITED, C.SERVER_ERROR])
    def test_transient_failures_may_be_retried(self, category):
        info = describe(category)
        assert info.retryable is True and info.transient is True

    def test_no_explanation_says_the_site_is_non_compliant(self):
        for c in C:
            text = (describe(c).explanation + describe(c).next_action).lower()
            assert "non-compliant" not in text and "violation" not in text
        for c in (C.TLS_CERTIFICATE_EXPIRED, C.TLS_CERTIFICATE_INVALID):
            assert "never turned off" in describe(c).explanation, "certificate checking is never relaxed to make a scan succeed"


@pytest.mark.parametrize("message,expected", [
    ("The robots.txt of www.facebook.com does not allow the scanner to visit https://www.facebook.com (or any page found on it), so no page was scanned", C.ROBOTS_TXT_DISALLOWED),
    ("Could not read the robots.txt of x.test (it answered HTTP 503), so it cannot be determined which pages the site permits", C.ROBOTS_TXT_UNAVAILABLE),
    ("Could not read the robots.txt of x.test (Timeout 5000ms exceeded), so it cannot be determined", C.ROBOTS_TXT_UNAVAILABLE),
    # an older scan stored a certificate failure as an unreadable robots.txt; the cause is the certificate
    ("Could not read the robots.txt of uat.consiva.ai (APIRequestContext.get: certificate has expired), so it cannot be determined which pages", C.TLS_CERTIFICATE_EXPIRED),
    ("Could not reach the site at all: https://x.test/ could not be loaded (net::ERR_CERT_DATE_INVALID).", C.TLS_CERTIFICATE_EXPIRED),
    ("net::ERR_CERT_AUTHORITY_INVALID at https://x.test/", C.TLS_CERTIFICATE_INVALID),
    ("APIRequestContext.get: self signed certificate", C.TLS_CERTIFICATE_INVALID),
    ("APIRequestContext.get: Hostname/IP does not match certificate's altnames", C.TLS_CERTIFICATE_INVALID),
    ("the domain name of https://nosuch.example/ did not resolve on any of 4 attempts (net::ERR_NAME_NOT_RESOLVED)", C.DNS_RESOLUTION_FAILED),
    ("getaddrinfo ENOTFOUND nosuch.example", C.DNS_RESOLUTION_FAILED),
    ("https://x.test/ closed the connection to the scanner on every one of 4 attempts (net::ERR_CONNECTION_RESET)", C.CONNECTION_FAILED),
    ("net::ERR_CONNECTION_REFUSED at https://x.test/", C.CONNECTION_FAILED),
    ("https://x.test/ kept answering with HTTP 429 (temporarily unavailable) after 6 attempts", C.RATE_LIMITED),
    ("https://x.test/ answered with HTTP 401 instead of a page", C.AUTHENTICATION_REQUIRED),
    ("https://x.test/ answered with HTTP 403 instead of a page. The site is refusing the scanner (access denied)", C.HTTP_ACCESS_DENIED),
    ("https://x.test/ kept answering with HTTP 503 (temporarily unavailable) after 6 attempts", C.SERVER_ERROR),
    ("https://x.test/ was too slow or unavailable over the whole retry window (10 attempts; Timeout 30000ms exceeded)", C.REQUEST_TIMEOUT),
    ("Could not reach the site at all: https://x.test/ could not be loaded (net::ERR_FAILED)", C.BROWSER_NAVIGATION_FAILED),
    ("something nobody has seen before", C.UNKNOWN_SCAN_ERROR),
])
def test_a_stored_error_text_is_named_the_same_way_whatever_the_site(message, expected):
    assert classify_message(message, url="https://x.test/").category == expected


def test_no_error_is_no_failure():
    assert classify_message(None) is None and classify_message("") is None and classify_message("   ") is None


class TestFromTheExceptionsTheScannerRaises:
    def test_the_scanners_own_exceptions_carry_their_category(self):
        assert classify_exception(RobotsUnreadable("x.test", "HTTP 503")).category == C.ROBOTS_TXT_UNAVAILABLE
        assert classify_exception(ScanNotPossible("The robots.txt of x does not allow the scanner", "robots_disallowed")).category == C.ROBOTS_TXT_DISALLOWED
        assert classify_exception(certificate_failure("https://x.test/", "certificate has expired")).category == C.TLS_CERTIFICATE_EXPIRED
        assert classify_exception(certificate_failure("https://x.test/", "self signed certificate")).category == C.TLS_CERTIFICATE_INVALID

    def test_a_seed_failure_is_named_from_its_cause(self):
        for error, expected in [("net::ERR_NAME_NOT_RESOLVED", C.DNS_RESOLUTION_FAILED), ("net::ERR_CONNECTION_RESET", C.CONNECTION_FAILED), ("net::ERR_CERT_DATE_INVALID", C.TLS_CERTIFICATE_EXPIRED)]:
            assert classify_exception(explain_seed_failure("https://x.test/", PlaywrightError(error), 3)).category == expected, error

    def test_anything_else_is_unknown_and_keeps_what_it_said(self):
        info = classify_exception(ValueError("odd"))
        assert info.category == C.UNKNOWN_SCAN_ERROR and info.detail == "odd"

    def test_many_sites_with_the_same_cause_get_the_same_handling(self):
        infos = [classify_exception(certificate_failure(f"https://site{i}.example/", "certificate has expired")) for i in range(5)]
        assert len({(i.category, i.retryable, i.owner, i.next_action) for i in infos}) == 1


class TestTheLifecycleWord:
    def test_the_in_flight_states(self):
        assert scan_outcome("queued") == QUEUED and scan_outcome("running") == RUNNING and scan_outcome("cancelled") == CANCELLED

    def test_a_completed_scan_that_assessed_no_page_is_never_completed(self):
        assert scan_outcome("completed", pages_scanned=0) == FAILED
        assert scan_outcome("completed", pages_scanned=0, error="The robots.txt of x does not allow the scanner to visit") == BLOCKED

    def test_a_clean_completed_scan(self):
        assert scan_outcome("completed", pages_scanned=5) == COMPLETED
        assert scan_outcome("completed", pages_scanned=5, warnings=True) == COMPLETED_WITH_WARNINGS

    def test_pages_that_could_not_be_assessed_make_it_partial(self):
        assert scan_outcome("completed", pages_scanned=3, pages_not_assessed=2) == PARTIALLY_COMPLETED
        assert scan_outcome("completed", pages_scanned=3, pages_not_assessed=2, warnings=True) == PARTIALLY_COMPLETED

    @pytest.mark.parametrize("error,expected", [
        ("The robots.txt of x does not allow the scanner to visit https://x/", BLOCKED),
        ("Could not read the robots.txt of x (HTTP 503), so it cannot be determined", BLOCKED),
        ("https://x/ answered with HTTP 403 instead of a page", BLOCKED),
        ("https://x/ answered with HTTP 401 instead of a page", BLOCKED),
        ("certificate has expired", FAILED), ("did not resolve", FAILED), ("closed the connection", FAILED), ("odd", FAILED),
    ])
    def test_a_failed_scan_is_blocked_when_access_was_withheld_and_failed_when_it_broke(self, error, expected):
        assert scan_outcome("failed", error=error) == expected

    def test_an_unknown_status_with_an_error_is_a_failure_not_a_success(self):
        assert scan_outcome("weird", error="boom") == FAILED
