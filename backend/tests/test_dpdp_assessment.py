"""The assessment record (scope, timestamp, versions), the prioritised remediation plan, the caution on thin scores, the legal mapping
as a versioned table, consent states, incomplete scans, reproducibility, and hand-reviewed golden examples."""

from dataclasses import asdict
from datetime import date
from typing import ClassVar

import pytest

from app.api.v1.routes.consent_scans import DpdpScoreResponse
from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import AFTER, TODAY, evidence, tr


def t(i, page="https://x.test/p", **kw):
    return {**tr(i, **kw), "page_url": page}


def by_id(score, rid):
    return next(r for r in score.requirements if r.id == rid)


POL = ("privacy_policy", "cookie_policy")


class TestAssessmentRecord:
    def test_it_records_the_main_domain_the_pages_the_limit_and_the_timestamp(self):
        a = assess(evidence(pages=3), TODAY, scan_url="https://www.Example.com/pricing?x=1", scanned_at="2026-10-21T06:41:50+00:00").assessment
        assert a.main_domain == "example.com" and a.scan_url == "https://www.Example.com/pricing?x=1"
        assert a.scanned_at == "2026-10-21T06:41:50+00:00" and a.assessed_on == TODAY
        assert a.pages_scanned == ["https://x.test/0", "https://x.test/1", "https://x.test/2"] and a.page_limit == 5

    def test_a_bare_host_works_and_no_url_is_allowed(self):
        assert assess(evidence(), TODAY, scan_url="example.org").assessment.main_domain == "example.org"
        a = assess(evidence(), TODAY).assessment
        assert a.main_domain is None and a.scanned_at is None

    def test_describing_the_scan_never_changes_a_score(self):
        plain = assess(evidence(trackers=[t(1)], pages=3), TODAY)
        described = assess(evidence(trackers=[t(1)], pages=3), TODAY, scan_url="https://x.test", scanned_at="2026-01-01T00:00:00+00:00")
        assert asdict(plain.readiness) == asdict(described.readiness)
        assert [r.status for r in plain.requirements] == [r.status for r in described.requirements]

    def test_it_says_it_is_website_readiness_not_full_legal_compliance(self):
        note = assess(evidence(), TODAY).assessment.nature_note
        assert "website readiness" in note and "not a guarantee" in note and "full legal compliance" in note

    def test_the_methodology_and_legal_mapping_versions_are_separate(self):
        s = assess(evidence(), TODAY)
        assert s.methodology.version == ds.METHODOLOGY_VERSION == "1.6.1"
        assert s.methodology.legal_mapping_version == ds.LEGAL_MAPPING_VERSION == "2026.10.1"
        assert s.methodology.version != s.methodology.legal_mapping_version

    def test_the_legal_sources_name_the_gazette_notifications(self):
        text = " ".join(f"{x['name']} {x['reference']}" for x in assess(evidence(), TODAY).methodology.legal_sources)
        for needle in ("G.S.R. 846(E)", "G.S.R. 892(E)", "G.S.R. 843(E)", "13 May 2027", "13 Nov 2026"):
            assert needle in text


class TestConsentStatesTested:
    def test_all_three_states_when_the_banner_was_operated_and_reject_confirmed(self):
        a = assess(evidence(pages=3), TODAY).assessment
        assert a.states_tested == ["Before Consent", "After Accept", "After Reject"] and a.states_not_tested == []

    def test_reject_that_was_not_confirmed_is_not_a_tested_state(self):
        a = assess(evidence(pages=3, confirmed=False), TODAY).assessment
        assert a.states_tested == ["Before Consent", "After Accept"] and a.states_not_tested == ["After Reject"]

    def test_a_banner_that_could_not_be_operated_tests_only_before_consent(self):
        a = assess(evidence(pages=3, accept_outcome="cmp_not_automatable", reject_outcome="cmp_not_automatable"), TODAY).assessment
        assert a.states_tested == ["Before Consent"] and a.states_not_tested == ["After Accept", "After Reject"]

    def test_with_no_pages_nothing_was_tested(self):
        a = assess(evidence(pages=0), TODAY).assessment
        assert a.states_tested == [] and len(a.states_not_tested) == 3

    def test_a_scan_that_predates_three_state_testing_did_not_test_before_consent(self):
        assert "Before Consent" in assess(evidence(pages=3, three_pass=False), TODAY).assessment.states_not_tested

    def test_findings_carry_the_consent_state_they_were_seen_in(self):
        g = assess(evidence(trackers=[t(1, states=("pre_consent",)), t(2, states=("post_reject",))], pages=3), AFTER).finding_groups
        states = {e.id: e.consent_states for e in g.confirmed}
        assert states["DPDP-C1"] == ["Before Consent"] and states["DPDP-C3"] == ["After Reject"]

    def test_a_request_seen_only_after_accept_is_never_a_before_consent_finding(self):
        s = assess(evidence(trackers=[t(1, states=("post_accept",))], pages=3, policies=POL), AFTER)
        assert by_id(s, "DPDP-C1").status == "met" and s.finding_groups.confirmed == []


class TestLegalMappingIsVersioned:
    # The mapping a reviewer signed off. Changing any citation, effective date or applicability means bumping LEGAL_MAPPING_VERSION
    # and this table together; this test fails until both are done.
    GOLDEN: ClassVar[dict] = {
        "DPDP-C1": ("2027-05-13", ["DPDP Act, 2023 s.6(1)", "DPDP Rules, 2025 r.3"]),
        "DPDP-C2": ("2027-05-13", ["DPDP Act, 2023 s.5(1)", "DPDP Act, 2023 s.6(1)"]),
        "DPDP-C3": ("2027-05-13", ["DPDP Act, 2023 s.6(1)", "DPDP Act, 2023 s.6(4)", "DPDP Act, 2023 s.6(6)", "DPDP Rules, 2025 r.3(c)(i)"]),
        "DPDP-C4": ("2027-05-13", ["DPDP Act, 2023 s.6(4)", "DPDP Rules, 2025 r.3(c)(i)"]),
        "DPDP-C5": ("2027-05-13", ["DPDP Act, 2023 s.6(1)", "DPDP Rules, 2025 r.3(b)(ii)"]),
        "DPDP-N1": ("2027-05-13", ["DPDP Act, 2023 s.5(1)", "DPDP Rules, 2025 r.3(a)", "DPDP Rules, 2025 r.3(b)"]),
        "DPDP-N2": ("2027-05-13", ["DPDP Act, 2023 s.5(1)", "DPDP Rules, 2025 r.3(b)(i)"]),
        "DPDP-R9": ("2027-05-13", ["DPDP Act, 2023 s.8(9)", "DPDP Rules, 2025 r.9"]),
        "DPDP-R14A": ("2027-05-13", ["DPDP Act, 2023 s.11", "DPDP Act, 2023 s.12", "DPDP Act, 2023 s.13", "DPDP Rules, 2025 r.14(1)"]),
        "DPDP-R14B": ("2027-05-13", ["DPDP Act, 2023 s.8(10)", "DPDP Act, 2023 s.13", "DPDP Rules, 2025 r.14(3)"]),
        "DPDP-S6": ("2027-05-13", ["DPDP Act, 2023 s.8(5)", "DPDP Rules, 2025 r.6"]),
        "DPDP-S7": ("2027-05-13", ["DPDP Act, 2023 s.8(6)", "DPDP Rules, 2025 r.7"]),
        "DPDP-X4": ("2026-11-13", ["DPDP Act, 2023 s.6(9)", "DPDP Rules, 2025 r.4"]),
        "DPDP-X8": ("2027-05-13", ["DPDP Act, 2023 s.8(7)", "DPDP Rules, 2025 r.8"]),
        "DPDP-X10": ("2027-05-13", ["DPDP Act, 2023 s.9", "DPDP Rules, 2025 r.10", "DPDP Rules, 2025 r.11"]),
        "DPDP-X13": ("2027-05-13", ["DPDP Act, 2023 s.10", "DPDP Rules, 2025 r.13"]),
    }

    def test_the_mapping_equals_the_reviewed_table_for_this_legal_mapping_version(self):
        actual = {r.id: (r.effective.isoformat(), list(r.provisions)) for r in ds.REQUIREMENTS}
        assert actual == self.GOLDEN
        assert ds.LEGAL_MAPPING_VERSION == "2026.10.1"

    def test_the_commencement_dates_follow_the_gazette(self):
        assert ds.PUBLICATION_DATE == date(2025, 11, 13)
        assert ds.EFFECTIVE_12_MONTHS == date(2026, 11, 13) and ds.EFFECTIVE_18_MONTHS == date(2027, 5, 13)

    def test_applicability_conditions_are_recorded_for_every_requirement(self):
        assert set(ds.APPLICABILITY_TEXT) == set(self.GOLDEN)

    def test_every_result_is_traceable_to_its_provisions_and_to_scan_evidence(self):
        s = assess(evidence(trackers=[t(1)], pages=3), TODAY)
        for r in s.requirements:
            assert r.provisions and r.description and r.criteria and r.expected_evidence
        c1 = by_id(s, "DPDP-C1")
        assert c1.evidence_ids == ["t1"] and c1.pages == ["https://x.test/p"]


class TestPrioritisedRemediation:
    def test_it_lists_the_actions_of_every_gap_in_a_fixed_order(self):
        s = assess(evidence(trackers=[t(1)], reject=False, granular=False, policies=("privacy_policy",), pages=3), TODAY)
        plan = s.remediation_plan
        assert [p.requirement_id for p in plan] == ["DPDP-C1", "DPDP-C4", "DPDP-N2"]
        assert [p.rank for p in plan] == [1, 2, 3]
        assert all(p.action and p.how_to_verify and p.legal_references and p.priority_reason for p in plan)

    def test_duties_in_force_come_before_future_readiness_and_are_labelled_fix_now(self):
        s = assess(evidence(trackers=[t(1)], pages=3, policies=("privacy_policy",)), AFTER)
        assert [p.priority for p in s.remediation_plan] == ["Fix now", "Improve"]
        assert s.remediation_plan[0].timing == "Current"

    def test_future_gaps_say_by_when_to_prepare(self):
        p = assess(evidence(trackers=[t(1)], pages=3, policies=POL), TODAY).remediation_plan[0]
        assert p.priority == "Prepare before 2027-05-13" and p.timing == "Future readiness"

    def test_not_met_comes_before_partially_met_and_heavier_before_lighter(self):
        plan = assess(evidence(trackers=[t(1)], reject=False, granular=False, policies=("privacy_policy",), pages=3), AFTER).remediation_plan
        assert [p.outcome for p in plan] == ["Not Met", "Not Met", "Partially Met"]
        assert plan[0].requirement_id == "DPDP-C1"          # weight 5 before weight 4

    def test_unverified_not_applicable_and_duplicate_checks_have_no_remediation_step(self):
        s = assess(evidence(trackers=[t(1)], mechanism="none", policies=(), pages=3), TODAY)
        ids = [p.requirement_id for p in s.remediation_plan]
        assert "DPDP-C2" not in ids and "DPDP-N2" not in ids and "DPDP-S6" not in ids

    def test_no_gaps_means_an_empty_plan(self):
        assert assess(evidence(pages=3), TODAY).remediation_plan == []

    def test_the_order_is_reproducible(self):
        a = assess(evidence(trackers=[t(1)], reject=False, pages=3), TODAY).remediation_plan
        b = assess(evidence(trackers=[t(1)], reject=False, pages=3), TODAY).remediation_plan
        assert a == b

    def test_configured_weights_do_not_reorder_by_themselves(self):
        a = [p.requirement_id for p in assess(evidence(trackers=[t(1)], reject=False, pages=3), TODAY).remediation_plan]
        b = [p.requirement_id for p in assess(evidence(trackers=[t(1)], reject=False, pages=3), TODAY, weights={"DPDP-C4": 10}).remediation_plan]
        assert a == b


class TestCautionOnThinScores:
    def test_a_score_resting_on_two_requirements_carries_a_caution(self):
        r = assess(evidence(pages=3, mechanism="none"), TODAY).readiness
        assert r.status == "scored" and "Treat this score with caution" in r.caution and "2 assessed requirements" in r.caution

    def test_low_coverage_carries_a_caution(self):
        r = assess(evidence(pages=3), TODAY).readiness            # 57.6% coverage, 5 requirements: no caution
        assert r.caution is None
        low = assess(evidence(pages=3), TODAY, ds.Applicability(True, True, True, True)).readiness     # coverage falls below 50%
        assert low.coverage_percent < 50 and "caution" in low.caution

    def test_a_withheld_score_has_no_caution_only_a_reason(self):
        r = assess(evidence(pages=0), TODAY).readiness
        assert r.caution is None and r.insufficient_reason

    def test_the_caution_never_changes_the_score(self):
        base = assess(evidence(pages=3, mechanism="none"), TODAY).readiness
        assert base.score_percent == 100.0 and base.caution


class TestIncompleteScans:
    def test_a_scan_with_no_pages_has_no_score_no_gaps_and_a_scope_that_says_nothing_was_tested(self):
        s = assess(evidence(pages=0), AFTER, scan_url="https://x.test")
        assert s.readiness.score_percent is None and s.finding_groups.confirmed == [] and s.remediation_plan == []
        assert s.assessment.states_tested == [] and s.assessment.pages_scanned == []

    def test_a_framed_wrapper_is_not_scored_and_says_why(self):
        s = assess(evidence(framed=["https://real.test"], trackers=[], policies=(), pages=1, mechanism="none"), AFTER)
        assert s.readiness.status == "insufficient_evidence" and s.remediation_plan == []

    def test_a_banner_not_shown_to_the_scanner_leaves_the_banner_checks_unverified(self):
        s = assess(evidence(mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                            reject_outcome="cmp_not_automatable", trackers=[t(1, states=())], pages=3), TODAY)
        assert [by_id(s, i).status for i in ("DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5")] == ["unable_to_verify"] * 4


class TestReproducible:
    def test_the_same_recorded_evidence_gives_an_identical_result(self):
        ev = evidence(trackers=[t(1), t(2, category=None)], reject=False, pages=4)
        assert asdict(assess(ev, TODAY, scan_url="https://x.test", scanned_at="2026-10-21T00:00:00+00:00")) == asdict(
            assess(ev, TODAY, scan_url="https://x.test", scanned_at="2026-10-21T00:00:00+00:00"))

    def test_the_result_serialises_through_the_api_model(self):
        m = DpdpScoreResponse(**asdict(assess(evidence(trackers=[t(1)], pages=3), TODAY, scan_url="https://x.test",
                                              scanned_at="2026-10-21T00:00:00+00:00")))
        assert m.assessment.main_domain == "x.test" and m.remediation_plan and m.methodology.legal_mapping_version


class TestHandReviewedGoldenExamples:
    """Scenarios reviewed by hand against the real sites they are modelled on. Each expected number is worked out on paper below,
    independently of the code, from the weights: C1 5, C2 4, C3 4, C4 4, C5 2, N1 4, N2 2; business checks R9 2, R14A 3, R14B 3, S6 3, S7 3."""

    def test_a_site_with_no_tracking_and_no_banner_but_a_privacy_policy(self):
        # agentmira.ai: C1 Met 5, N1 Met 4; C2..C5 and N2 Not Applicable. Earned 9 of 9 = 100.0%.
        # Applicable = 9 + 14 business = 23; coverage = 9 / 23 = 39.1%. Two requirements only: a caution.
        s = assess(evidence(pages=3, mechanism="none", policies=("privacy_policy",)), TODAY)
        assert (s.readiness.points_earned, s.readiness.points_assessed, s.readiness.score_percent) == (9, 9, 100.0)
        assert (s.readiness.applicable_weight, s.readiness.coverage_percent) == (23, 39.1) and s.readiness.caution

    def test_a_tracking_site_with_no_banner_and_no_policy(self):
        # digitalglobal.ai: C1 Not Met 0/5; N1 Not Met 0/4; C2 and N2 are duplicates of those. Earned 0 of 9 = 0.0%; coverage 39.1%.
        s = assess(evidence(trackers=[t(i) for i in range(1, 7)], pages=1, mechanism="none", policies=()), TODAY)
        assert (s.readiness.points_earned, s.readiness.points_assessed, s.readiness.score_percent) == (0, 9, 0.0)
        assert s.readiness.coverage_percent == 39.1
        assert by_id(s, "DPDP-C2").duplicate_of == "DPDP-C1" and by_id(s, "DPDP-N2").duplicate_of == "DPDP-N1"

    def test_a_clean_site_with_a_working_consent_tool(self):
        # swaransoft.com: C1 5, C2 4, C3 4, C4 4, C5 2, N1 4, N2 2 all Met = 25 of 25 = 100.0%. Applicable = 25 + 14 = 39; coverage 64.1%.
        s = assess(evidence(trackers=[t(1, states=("post_accept",))], pages=2, policies=POL), TODAY)
        assert (s.readiness.points_earned, s.readiness.points_assessed, s.readiness.score_percent) == (25, 25, 100.0)
        assert (s.readiness.applicable_weight, s.readiness.coverage_percent) == (39, 64.1)

    def test_a_site_that_tracks_before_consent_and_whose_reject_could_not_be_tested(self):
        # consiva.ai: C1 Not Met 0/5; C2 Met 4; C3 Unable to Verify (Reject not automatable); C4 Met 4; C5 Met 2; N1 Met 4; N2 Met 2.
        # Earned 16 of 21 = 76.2%. Applicable = 21 + 4 (C3 unverified) + 14 = 39; coverage = 21 / 39 = 53.8%.
        s = assess(evidence(trackers=[t(1)], pages=3, policies=POL, reject_outcome="cmp_not_automatable", confirmed=False), TODAY)
        assert (s.readiness.points_earned, s.readiness.points_assessed, s.readiness.score_percent) == (16, 21, 76.2)
        assert (s.readiness.applicable_weight, s.readiness.coverage_percent) == (39, 53.8)
        assert by_id(s, "DPDP-C3").status == "unable_to_verify"

    def test_a_framed_wrapper_is_not_scored_whatever_it_shows(self):
        # A frameset wrapper whose own requests were not classified as analytics or marketing: every observable check is Unable to Verify.
        # Nothing assessed: Insufficient Evidence, coverage 0%.
        s = assess(evidence(framed=["https://corp.gignaati.com"], trackers=[t(i, category=None) for i in range(1, 5)], policies=(), pages=1, mechanism="none"), TODAY)
        assert s.readiness.score_percent is None and s.readiness.coverage_percent == 0.0

    def test_a_lone_observed_failure_on_a_wrapper_never_becomes_a_number(self):
        s = assess(evidence(framed=["https://corp.gignaati.com"], trackers=[t(i) for i in range(1, 5)], policies=(), pages=1, mechanism="none"), TODAY)
        assert by_id(s, "DPDP-C1").status == "not_met"
        assert s.readiness.score_percent is None and s.readiness.status == "insufficient_evidence"   # one assessed requirement: no score, not 0%

    @pytest.mark.parametrize("kw,expected", [
        ({"pages": 3, "mechanism": "none", "policies": ("privacy_policy",)}, 100.0),
        ({"trackers": [tr(1)], "pages": 3, "mechanism": "none", "policies": ()}, 0.0),
    ])
    def test_scores_are_stable_across_reruns(self, kw, expected):
        assert assess(evidence(**kw), TODAY).readiness.score_percent == expected == assess(evidence(**kw), AFTER).readiness.score_percent
