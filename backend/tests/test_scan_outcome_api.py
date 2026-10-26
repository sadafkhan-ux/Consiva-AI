"""What the APIs say about how a scan ended: the lifecycle word, and for a scan that could not complete, why, whether retrying helps, and what to do. Derived from stored fields only."""

from types import SimpleNamespace

import pytest

from app.api.v1.schemas.consent_agent import ScanStatusResponse
from app.services.consent_agent_api_service import failure_and_outcome

URL = "https://x.test/"


def scan(status="completed", error=None):
    return SimpleNamespace(status=status, error=error, url=URL)


def stages(**meta):
    return [SimpleNamespace(stage="website_scan", status="completed", stage_metadata=meta)]


CLEAN = {
    "pages_found": 5, "pages_discovered": 40, "pages_scanned": 5, "pages_failed": 0, "pages_timeout": 0, "pages_blocked_robots": 0, "accept_interaction": "clicked",
    "reject_interaction": "clicked", "reject_click_confirmed": True, "stop_reason": "page_limit",
}


class TestAScanThatEnded:
    def test_a_clean_scan_is_completed_with_no_failure(self):
        out = failure_and_outcome(scan(), stages(**CLEAN))
        assert out == {"outcome": "COMPLETED", "failure": None}

    def test_an_untested_consent_path_is_a_warning_not_a_failure(self):
        out = failure_and_outcome(scan(), stages(**{**CLEAN, "accept_interaction": "cmp_not_automatable", "reject_interaction": "cmp_not_automatable"}))
        assert out["outcome"] == "COMPLETED_WITH_WARNINGS" and out["failure"] is None

    def test_an_early_stop_is_a_warning(self):
        assert failure_and_outcome(scan(), stages(**{**CLEAN, "stop_reason": "slow_site"}))["outcome"] == "COMPLETED_WITH_WARNINGS"

    def test_pages_that_failed_make_it_partial_and_the_failure_says_how_many(self):
        out = failure_and_outcome(scan(), stages(**{**CLEAN, "pages_scanned": 3, "pages_failed": 1, "pages_timeout": 1}))
        assert out["outcome"] == "PARTIALLY_COMPLETED"
        assert out["failure"]["category"] == "PARTIAL_SCAN" and "3 scanned" in out["failure"]["detail"] and "1 failed" in out["failure"]["detail"] and "1 timed out" in out["failure"]["detail"]

    def test_pages_refused_by_robots_make_it_partial(self):
        assert failure_and_outcome(scan(), stages(**{**CLEAN, "pages_scanned": 4, "pages_blocked_robots": 2}))["outcome"] == "PARTIALLY_COMPLETED"

    def test_a_completed_scan_that_assessed_no_page_is_not_completed(self):
        assert failure_and_outcome(scan(), stages(**{**CLEAN, "pages_scanned": 0, "pages_found": 0}))["outcome"] == "FAILED"


class TestAScanThatDidNotComplete:
    @pytest.mark.parametrize("error,outcome,category,retryable,owner", [
        ("The robots.txt of www.facebook.com does not allow the scanner to visit https://www.facebook.com (or any page found on it), so no page was scanned", "BLOCKED", "ROBOTS_TXT_DISALLOWED", False, "website_owner"),
        ("Could not reach the site at all: https://uat.consiva.ai/ does not present a valid security certificate (certificate has expired).", "FAILED", "TLS_CERTIFICATE_EXPIRED", False, "website_owner"),
        ("Could not read the robots.txt of x.test (it answered HTTP 503), so it cannot be determined which pages the site permits", "BLOCKED", "ROBOTS_TXT_UNAVAILABLE", True, "none"),
        ("https://x.test/ answered with HTTP 401 instead of a page", "BLOCKED", "AUTHENTICATION_REQUIRED", False, "website_owner"),
        ("https://x.test/ kept answering with HTTP 429 (temporarily unavailable) after 6 attempts", "FAILED", "RATE_LIMITED", True, "none"),
        ("net::ERR_NAME_NOT_RESOLVED", "FAILED", "DNS_RESOLUTION_FAILED", False, "website_owner"),
    ])
    def test_each_failure_is_named_with_what_to_do(self, error, outcome, category, retryable, owner):
        out = failure_and_outcome(scan("failed", error), [])
        assert out["outcome"] == outcome
        f = out["failure"]
        assert f["category"] == category and f["retryable"] is retryable and f["owner"] == owner and f["next_action"] and f["explanation"] and f["detail"] and f["url"] == URL

    def test_the_underlying_error_is_kept_not_replaced_by_a_generic_finding(self):
        out = failure_and_outcome(scan("failed", "net::ERR_CERT_AUTHORITY_INVALID at https://x.test/"), [])
        assert "ERR_CERT_AUTHORITY_INVALID" in out["failure"]["detail"]

    def test_several_sites_with_one_cause_get_one_handling(self):
        outs = [failure_and_outcome(SimpleNamespace(status="failed", error="certificate has expired", url=f"https://s{i}.test/"), []) for i in range(4)]
        assert len({(o["outcome"], o["failure"]["category"], o["failure"]["next_action"]) for o in outs}) == 1


class TestTheInFlightAndHistoricalCases:
    @pytest.mark.parametrize("status,outcome", [("pending", "QUEUED"), ("queued", "QUEUED"), ("running", "RUNNING"), ("cancelled", "CANCELLED")])
    def test_in_flight_states(self, status, outcome):
        assert failure_and_outcome(scan(status), [])["outcome"] == outcome

    def test_an_old_completed_scan_with_no_stage_record_is_not_called_completed_on_a_guess(self):
        out = failure_and_outcome(scan("completed"), [])
        assert out["outcome"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "RUNNING", "FAILED") and out["failure"] is None

    def test_a_failed_scan_whose_error_was_never_recorded_is_failed_with_no_invented_cause(self):
        out = failure_and_outcome(scan("failed", None), [])
        assert out["outcome"] == "FAILED" and out["failure"] is None


def test_the_public_status_response_accepts_the_new_fields_and_keeps_the_old_ones():
    from datetime import UTC, datetime
    from uuid import uuid4
    r = ScanStatusResponse(
        scan_id=uuid4(), website_url=URL, status="failed", progress=0, pages_discovered=0, pages_scanned=0, pages_failed=0, current_stage=None, started_at=datetime.now(UTC), completed_at=None,
        error={"code": "SCAN_FAILED", "message": "x", "failure": {"category": "RATE_LIMITED"}}, outcome="FAILED", failure={"category": "RATE_LIMITED"},
    )
    assert r.error["code"] == "SCAN_FAILED" and r.outcome == "FAILED" and r.failure["category"] == "RATE_LIMITED"
    old = ScanStatusResponse(scan_id=uuid4(), website_url=URL, status="queued", progress=0, pages_discovered=0, pages_scanned=0, pages_failed=0, current_stage=None, started_at=None, completed_at=None)
    assert old.outcome is None and old.failure is None
