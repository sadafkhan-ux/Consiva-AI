"""The "Logic Required / Human Review" content and the verified / missing / uncertain breakdown (app/scoring/review.py).

A technical signal is an observation, not a verdict: these tests hold the line between what the scan confirms and what a
person has to decide, and check that nothing is reported about other websites or subdomains.
"""

from dataclasses import asdict
from datetime import date

from app.api.v1.routes.consent_scans import DpdpScoreResponse
from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import TODAY, evidence, tr


def items(score):
    return {i.id: i for i in score.human_review.items}


class TestEvidenceSummary:
    def test_requests_known_trackers_and_cookies_are_counted_apart(self):
        cookie = {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": None, "source": "rule",
                  "consent_states": ["pre_consent"]}
        s = assess(evidence(trackers=[tr(1), tr(2, category=None), tr(3, category="functional", states=("post_accept",))],
                            cookies=[cookie]), TODAY).evidence_summary
        assert s.network_requests == 3
        assert s.known_tracker_requests == 1 and s.unclassified_requests == 1
        assert s.cookies == 1 and s.known_tracker_cookies == 1
        assert (s.network_requests_by_state.pre_consent, s.network_requests_by_state.post_accept) == (2, 1)
        assert s.known_tracker_requests_by_state.pre_consent == 1

    def test_the_summary_says_a_request_is_not_a_tracker_that_ran(self):
        note = assess(evidence(), TODAY).evidence_summary.note
        assert "does not show that a tracker ran" in note and "None of these counts is a finding" in note

    def test_pages_scanned_is_reported(self):
        assert assess(evidence(pages=4), TODAY).evidence_summary.pages_scanned == 4


class TestTechnicalSignalsAreNotAutomaticViolations:
    def test_known_tracker_before_consent_is_an_observation_that_needs_a_person(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        item = items(s)["known-tracker-before-consent"]
        assert item.category == "uncertain"
        assert "legal_interpretation" in item.needs and "business_context" in item.needs
        text = " ".join(item.known)
        assert "not proof that a tracker ran" in text
        assert any("personal data was actually sent" in m for m in item.missing)
        assert any("legitimate use" in m for m in item.missing)
        assert item.evidence_ids == ["t1"] and item.provisions

    def test_unclassified_requests_are_not_called_violations(self):
        s = assess(evidence(trackers=[tr(1, category=None, states=("pre_consent",))]), TODAY)
        item = items(s)["unclassified-before-consent"]
        assert item.needs == ["business_context"]
        assert "violation" not in " ".join(item.known + [item.decision]).lower()

    def test_unclassified_requests_seen_after_consent_still_go_to_review(self):
        s = assess(evidence(trackers=[tr(1, category=None, states=("post_accept",))]), TODAY)
        assert "unclassified-signals" in items(s)
        assert "does not treat them as violations" in items(s)["unclassified-signals"].decision

    def test_activity_after_reject_asks_for_confirmation(self):
        s = assess(evidence(trackers=[tr(1, states=("post_reject",))]), TODAY)
        item = items(s)["activity-after-reject"]
        assert item.category == "uncertain" and "clean browser profile" in item.decision

    def test_a_clean_site_has_no_violation_style_items(self):
        ids = set(items(assess(evidence(), TODAY)))
        assert ids == {"organisational-requirements", "entity-applicability", "not-yet-in-force"}


class TestMissingEvidence:
    def test_banner_not_displayed(self):
        s = assess(evidence(mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                            reject_outcome="cmp_not_automatable", trackers=[tr(1, states=())]), TODAY)
        item = items(s)["banner-not-displayed"]
        assert item.category == "missing_evidence" and "more_evidence" in item.needs
        assert "reject-untested" not in items(s)

    def test_reject_that_could_not_be_operated(self):
        s = assess(evidence(reject_outcome="click_failed"), TODAY)
        assert items(s)["reject-untested"].category == "missing_evidence"

    def test_framed_wrapper(self):
        s = assess(evidence(framed=["https://real.test"]), TODAY)
        item = items(s)["framed-wrapper"]
        assert item.category == "missing_evidence" and "directly" in item.decision

    def test_privacy_notice_not_found_asks_where_it_is_published(self):
        s = assess(evidence(policies=()), TODAY)
        item = items(s)["privacy-notice-not-found"]
        assert item.category == "uncertain" and "main-domain page" in item.known[0]
        assert any("elsewhere" in m for m in item.missing)

    def test_the_page_limit_is_disclosed_when_it_is_reached(self):
        assert "page-limit" in items(assess(evidence(pages=5), TODAY))
        assert "page-limit" not in items(assess(evidence(pages=4), TODAY))
        assert f"first {ds.MAX_WEB_PAGES} pages" in items(assess(evidence(pages=5), TODAY))["page-limit"].topic

    def test_organisational_requirements_are_listed_as_needing_organisational_evidence(self):
        item = items(assess(evidence(), TODAY))["organisational-requirements"]
        assert item.category == "missing_evidence"
        assert {"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"} <= set(item.requirement_ids)


class TestApplicabilityAndTiming:
    def test_entity_specific_duties_ask_the_business_to_declare(self):
        item = items(assess(evidence(), TODAY))["entity-applicability"]
        assert "business_context" in item.needs and "legal_interpretation" in item.needs
        assert set(item.requirement_ids) == {"DPDP-X4", "DPDP-X8", "DPDP-X10", "DPDP-X13"}

    def test_declared_duties_stop_being_listed_as_undeclared(self):
        s = assess(evidence(), TODAY, ds.Applicability(True, True, True, True))
        assert "entity-applicability" not in items(s)

    def test_not_yet_in_force_is_a_legal_interpretation_item(self):
        item = items(assess(evidence(), TODAY))["not-yet-in-force"]
        assert item.needs == ["legal_interpretation"] and "2027-05-13" in item.known[0] and "readiness" in item.known[0]

    def test_it_disappears_once_the_duties_apply(self):
        assert "not-yet-in-force" not in items(assess(evidence(), date(2027, 5, 13)))


class TestBreakdown:
    def test_verified_gaps_partial_and_missing_follow_the_requirement_statuses(self):
        s = assess(evidence(trackers=[tr(1)], reject=False, granular=False), TODAY)
        b = s.breakdown
        assert b.verified_gaps == [], "the duties are not yet in force, so none is a current gap"
        assert [g.requirement_id for g in b.readiness_gaps] == ["DPDP-C1", "DPDP-C4"]
        assert [g.requirement_id for g in b.partially_satisfied] == ["DPDP-N2"]
        assert {m.requirement_id for m in b.missing_evidence} == {"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"}
        assert b.uncertain_count == sum(1 for i in s.human_review.items if i.category == "uncertain")

    def test_a_gap_carries_what_the_scan_observed_and_its_evidence(self):
        gap = assess(evidence(trackers=[tr(1)]), TODAY).breakdown.readiness_gaps[0]
        assert gap.evidence_ids == ["t1"] and gap.rule_ids and gap.provisions and gap.what_the_scan_observed

    def test_missing_evidence_separates_what_a_scan_could_see_from_what_it_never_can(self):
        s = assess(evidence(reject_outcome="click_failed"), TODAY)
        kinds = {m.requirement_id: m.kind for m in s.breakdown.missing_evidence}
        assert kinds["DPDP-C3"] == "scan_limit" and kinds["DPDP-S6"] == "organisational"

    def test_suppressed_duplicates_and_not_applicable_are_in_none_of_the_lists(self):
        s = assess(evidence(reject=False, granular=False), TODAY)
        listed = {g.requirement_id for g in s.breakdown.verified_gaps + s.breakdown.readiness_gaps + s.breakdown.partially_satisfied}
        listed |= {m.requirement_id for m in s.breakdown.missing_evidence}
        assert "DPDP-C5" not in listed and "DPDP-X10" not in listed


class TestStatementAndScope:
    def test_the_box_never_claims_complete_compliance(self):
        for ev in (evidence(), evidence(trackers=[tr(1)]), evidence(pages=0)):
            statement = assess(ev, TODAY).human_review.statement
            assert "cannot establish complete DPDP compliance" in statement
            assert "not proof that a tracker ran" in statement

    def test_nothing_is_reported_about_subdomains_or_other_websites(self):
        s = assess(evidence(trackers=[tr(1), tr(2, category=None)], framed=()), TODAY)
        blob = str(asdict(s)).lower()
        assert "subdomain" not in blob and "discovered" not in blob and "external website" not in blob

    def test_the_whole_result_serialises_through_the_api_model(self):
        m = DpdpScoreResponse(**asdict(assess(evidence(trackers=[tr(1)]), TODAY)))
        assert m.human_review.items and m.breakdown.readiness_gaps and m.evidence_summary.network_requests == 1

    def test_nothing_is_ever_scored_met_without_evidence(self):
        s = assess(evidence(pages=0), TODAY)
        assert not s.breakdown.verified_gaps and not s.breakdown.readiness_gaps and not s.breakdown.partially_satisfied
        assert len(s.breakdown.missing_evidence) >= 5
