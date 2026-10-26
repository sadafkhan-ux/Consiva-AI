"""The public API summary never says more than the scan established."""

import pytest

from app.services.consent_agent_api_service import (
    _compliance_status,
    _consent_states_tested,
    _page_stats,
)

NONE = {"critical": 0, "high": 0, "medium": 0, "low": 0}


def stage(**meta):
    return [type("S", (), {"stage": "website_scan", "stage_metadata": meta})()]


class TestComplianceStatus:
    def go(self, status="completed", sev=NONE, findings=False, pages=5, accept=True, reject=True):
        return _compliance_status(status, sev, findings, pages_scanned=pages, accept_tested=accept, reject_tested=reject)

    def test_compliant_only_when_the_scan_tested_and_found_nothing(self):
        assert self.go() == "compliant"

    @pytest.mark.parametrize("kw", [{"pages": 0}, {"accept": False}, {"reject": False}, {"accept": False, "reject": False}])
    def test_a_scan_that_could_not_look_is_not_compliant(self, kw):
        assert self.go(**kw) == "review_required"

    @pytest.mark.parametrize("status", ["failed", "running", "queued", "cancelled"])
    def test_a_scan_that_did_not_complete_is_unknown(self, status):
        assert self.go(status=status) == "unknown"

    def test_findings_still_decide(self):
        assert self.go(sev={**NONE, "high": 1}, findings=True) == "issues_found"
        assert self.go(sev={**NONE, "low": 1}, findings=True) == "review_required"


class TestPageCounts:
    def test_discovered_and_scanned_are_the_crawlers_figures_not_the_page_records(self):
        s = _page_stats(stage(pages_found=5, pages_failed=0, pages_discovered=562, pages_scanned=5))
        assert (s["pages_discovered"], s["pages_scanned"]) == (562, 5)

    def test_a_failed_page_is_not_a_scanned_page(self):
        s = _page_stats(stage(pages_found=5, pages_failed=2, pages_discovered=40, pages_scanned=3))
        assert (s["pages_discovered"], s["pages_scanned"], s["pages_failed"]) == (40, 3, 2)

    def test_scans_stored_before_those_figures_existed_keep_the_old_derivation(self):
        s = _page_stats(stage(pages_found=5, pages_failed=1))
        assert (s["pages_discovered"], s["pages_scanned"]) == (5, 4)


class TestConsentStatesTested:
    def test_a_failed_page_record_is_not_a_before_consent_observation(self):
        assert _consent_states_tested({}, 0)["before"] is False
        assert _consent_states_tested({}, 3)["before"] is True

    def test_reject_needs_the_banner_to_have_responded_like_the_score_requires(self):
        clicked = {"reject_interaction": "clicked"}
        assert _consent_states_tested({**clicked, "reject_click_confirmed": True}, 1)["reject"] is True
        assert _consent_states_tested({**clicked, "reject_click_confirmed": False}, 1)["reject"] is False, "dispatched, but the banner never observably responded"
        assert _consent_states_tested(clicked, 1)["reject"] is True, "a scan stored before the confirmation was kept reads as it always did"
        assert _consent_states_tested({"reject_interaction": "cmp_not_automatable", "reject_click_confirmed": True}, 1)["reject"] is False

    def test_accept_is_tested_only_when_clicked(self):
        assert _consent_states_tested({"accept_interaction": "clicked"}, 1)["accept"] is True
        for outcome in ("cmp_not_automatable", "click_failed", "not_found", None):
            assert _consent_states_tested({"accept_interaction": outcome}, 1)["accept"] is False

    def test_the_summary_verdict_follows_the_same_states(self):
        none = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        s = _consent_states_tested({"accept_interaction": "clicked", "reject_interaction": "clicked", "reject_click_confirmed": False}, 5)
        assert _compliance_status("completed", none, False, pages_scanned=5, accept_tested=s["accept"], reject_tested=s["reject"]) == "review_required"
