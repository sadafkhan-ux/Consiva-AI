"""Each DPDP check as a documented record: definition, legal reference, evidence record, timing, status counts, remediation
(app/scoring/dpdp_score.py DETAILS and RequirementResult). The score itself is tested in test_dpdp_score.py."""

from datetime import date

from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import AFTER, TODAY, evidence, tr


def by_id(score, rid):
    return next(r for r in score.requirements if r.id == rid)


class TestEveryCheckIsDefined:
    def test_every_requirement_has_a_unique_id_description_criteria_and_expected_evidence(self):
        ids = [r.id for r in ds.REQUIREMENTS]
        assert len(ids) == len(set(ids)) and set(ids) == set(ds.DETAILS)
        for rid, d in ds.DETAILS.items():
            assert d.description and d.criteria and d.expected_evidence, rid

    def test_every_check_a_scan_can_assess_explains_why_it_matters_how_to_fix_it_and_how_to_verify(self):
        for req in ds.REQUIREMENTS:
            if req.scan_assessable:
                d = ds.DETAILS[req.id]
                assert d.why_it_matters and d.remediation and d.verification, req.id

    def test_why_it_matters_restates_the_provision_and_does_not_invent_penalties(self):
        for req in ds.REQUIREMENTS:
            text = ds.DETAILS[req.id].why_it_matters
            if text:
                assert "Act s." in text or "Rules r." in text, req.id
                assert "fine" not in text.lower() and "penalt" not in text.lower() and "crore" not in text.lower(), req.id

    def test_the_criteria_name_the_status_each_outcome_leads_to(self):
        for rid in ("DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-N1", "DPDP-N2"):
            c = ds.DETAILS[rid].criteria
            assert "Met:" in c and "Not Met:" in c or "Unable to Verify" in c, rid

    def test_a_result_carries_its_definition(self):
        r = by_id(assess(evidence(), TODAY), "DPDP-C1")
        assert r.description == ds.DETAILS["DPDP-C1"].description and r.criteria and r.expected_evidence


class TestFiveStatusesOnly:
    def test_no_other_status_ever_appears(self):
        cases = [evidence(), evidence(trackers=[tr(1)], mechanism="none", policies=()), evidence(reject=False, granular=False),
                 evidence(pages=0), evidence(framed=["https://x.test"])]
        for ev in cases:
            for r in assess(ev, TODAY).requirements:
                assert r.status in ds.STATUSES, r.status

    def test_a_duplicate_is_not_applicable_with_a_documented_reason(self):
        r = by_id(assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY), "DPDP-C2")
        assert r.status == "not_applicable" and r.duplicate_of == "DPDP-C1"
        assert r.exclusion_reason.startswith("Counted under DPDP-C1")

    def test_every_not_applicable_has_a_reason(self):
        for r in assess(evidence(), TODAY).requirements:
            if r.status == "not_applicable":
                assert r.exclusion_reason, r.id


class TestStatusCounts:
    def test_counts_cover_every_requirement_and_add_up(self):
        s = assess(evidence(trackers=[tr(1)], reject=False, granular=False), TODAY)
        assert set(s.status_counts) == set(ds.STATUSES)
        assert sum(s.status_counts.values()) == len(s.requirements) == len(ds.REQUIREMENTS)
        assert s.status_counts == {"met": 3, "partially_met": 1, "not_met": 2, "unable_to_verify": 5, "not_applicable": 5}

    def test_untested_checks_are_counted_as_unable_to_verify_never_as_met(self):
        s = assess(evidence(pages=0), TODAY)
        assert s.status_counts["met"] == 0 and s.status_counts["unable_to_verify"] >= 5


class TestTimingReadinessVersusCurrent:
    def test_a_gap_in_a_future_duty_is_a_readiness_gap_not_a_current_violation(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        c1 = by_id(s, "DPDP-C1")
        assert c1.status == "not_met" and c1.timing == "future" and not c1.in_force
        assert [g.requirement_id for g in s.breakdown.readiness_gaps] == ["DPDP-C1"]
        assert s.breakdown.verified_gaps == []
        assert s.breakdown.readiness_gaps[0].applies_from == "2027-05-13"

    def test_the_same_gap_is_a_current_gap_once_the_duty_applies(self):
        s = assess(evidence(trackers=[tr(1)]), AFTER)
        assert [g.requirement_id for g in s.breakdown.verified_gaps] == ["DPDP-C1"] and s.breakdown.readiness_gaps == []
        assert by_id(s, "DPDP-C1").timing == "in_force"

    def test_the_day_before_it_applies_it_is_still_a_readiness_gap(self):
        s = assess(evidence(trackers=[tr(1)]), date(2027, 5, 12))
        assert s.breakdown.verified_gaps == [] and s.breakdown.readiness_gaps

    def test_the_formula_is_unchanged_by_timing(self):
        a = assess(evidence(trackers=[tr(1)]), TODAY).readiness
        b = assess(evidence(trackers=[tr(1)]), AFTER).readiness
        assert a.score_percent == b.score_percent and a.coverage_percent == b.coverage_percent


class TestEvidenceRecord:
    def cookie(self, pages):
        return {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": None, "source": "rule",
                "consent_states": ["pre_consent"],
                "observations": [{"consent_state": "pre_consent", "page_url": p, "method": "set-cookie"} for p in pages]}

    def test_it_records_what_was_expected_observed_where_and_whether_it_is_enough(self):
        t = {**tr(1), "page_url": "https://x.test/pricing"}
        r = by_id(assess(evidence(trackers=[t]), TODAY), "DPDP-C1")
        assert r.expected_evidence and r.observations and r.evidence_ids == ["t1"]
        assert r.pages == ["https://x.test/pricing"]
        assert r.sufficient is True and r.sufficiency_reason and "1 main-domain page(s)" in r.limitation

    def test_cookie_pages_come_from_their_recorded_sightings(self):
        r = by_id(assess(evidence(cookies=[self.cookie(["https://x.test/a", "https://x.test/b", "https://x.test/a"])]), TODAY), "DPDP-C1")
        assert r.pages == ["https://x.test/a", "https://x.test/b"]

    def test_a_check_with_no_item_evidence_names_the_pages_that_were_checked(self):
        r = by_id(assess(evidence(pages=3), TODAY), "DPDP-N1")
        assert r.pages == ["https://x.test/0", "https://x.test/1", "https://x.test/2"]

    def test_banner_checks_name_the_page_the_banner_was_tested_on(self):
        ev = evidence()
        ev["consent_signals"][0]["evidence"]["interaction_page_url"] = "https://x.test/home"
        assert by_id(assess(ev, TODAY), "DPDP-C4").pages == ["https://x.test/home"]

    def test_unable_to_verify_says_the_evidence_is_not_sufficient_and_why(self):
        r = by_id(assess(evidence(reject_outcome="click_failed"), TODAY), "DPDP-C3")
        assert r.status == "unable_to_verify" and r.sufficient is False and "Reject could not be exercised" in r.sufficiency_reason

    def test_organisational_checks_say_a_website_cannot_observe_them(self):
        r = by_id(assess(evidence(), TODAY), "DPDP-S6")
        assert r.sufficient is False and r.limitation == "A website scan cannot observe this." and r.pages == []

    def test_partially_met_says_the_evidence_supports_only_part(self):
        r = by_id(assess(evidence(trackers=[tr(1)]), TODAY), "DPDP-N2")
        assert r.status == "partially_met" and "only part" in r.sufficiency_reason

    def test_not_applicable_has_no_sufficiency_claim(self):
        r = by_id(assess(evidence(), TODAY), "DPDP-C2")
        assert r.status == "not_applicable" and r.sufficient is None

    def test_the_record_never_changes_a_status(self):
        before = [r.status for r in assess(evidence(trackers=[tr(1)]), TODAY).requirements]
        after = [r.status for r in assess(evidence(trackers=[tr(1)]), TODAY).requirements]
        assert before == after


class TestRemediation:
    def test_a_gap_says_what_was_observed_the_requirement_why_it_matters_the_action_and_the_check(self):
        gap = assess(evidence(trackers=[tr(1)]), TODAY).breakdown.readiness_gaps[0]
        assert gap.what_the_scan_observed and gap.requirement_id == "DPDP-C1" and gap.provisions
        assert gap.why_it_matters and gap.recommended_action and gap.how_to_verify
        assert "Act s.6(1)" in gap.why_it_matters and "Re-scan" in gap.how_to_verify

    def test_a_partly_met_requirement_also_gets_remediation(self):
        gap = assess(evidence(trackers=[tr(1)]), TODAY).breakdown.partially_satisfied[0]
        assert gap.requirement_id == "DPDP-N2" and gap.recommended_action and gap.how_to_verify

    def test_met_and_unverifiable_checks_carry_no_remediation(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        assert by_id(s, "DPDP-N1").remediation == "" and by_id(s, "DPDP-S6").remediation == ""

    def test_every_gap_in_the_breakdown_has_remediation(self):
        s = assess(evidence(trackers=[tr(1, states=("post_reject",)), tr(2)], reject=False, granular=False, policies=()), TODAY)
        for g in [*s.breakdown.verified_gaps, *s.breakdown.readiness_gaps, *s.breakdown.partially_satisfied]:
            assert g.recommended_action and g.how_to_verify and g.why_it_matters, g.requirement_id


class TestWebsiteChecksVersusBusinessChecks:
    def test_every_check_is_labelled_website_or_business(self):
        s = assess(evidence(), TODAY)
        types = {r.id: r.check_type for r in s.requirements}
        assert set(types.values()) == {"website", "business"}
        assert {i for i, t in types.items() if t == "website"} == {"DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5", "DPDP-N1", "DPDP-N2"}

    def test_a_business_check_can_never_be_met_by_a_scan(self):
        for ev in (evidence(), evidence(trackers=[tr(1)]), evidence(pages=0)):
            for r in assess(ev, TODAY, ds.Applicability(True, True, True, True)).requirements:
                if r.check_type == "business":
                    assert r.status == "unable_to_verify" and r.points_earned == 0 and r.confidence == "none", r.id

    def test_retention_dpia_grievance_and_breach_are_business_checks_not_claimed_complete(self):
        s = assess(evidence(), TODAY, ds.Applicability(True, True, True, True))
        for rid in ("DPDP-X8", "DPDP-X13", "DPDP-R14B", "DPDP-S7"):
            r = next(x for x in s.requirements if x.id == rid)
            assert r.check_type == "business" and r.status == "unable_to_verify" and r.sufficient is False

    def test_the_review_says_the_scan_makes_no_claim_about_ropa_dpia_retention_grievance_or_breach_times(self):
        item = next(i for i in assess(evidence(), TODAY).human_review.items if i.id == "organisational-requirements")
        text = " ".join(item.known)
        for word in ("retention", "DPIA", "grievance", "breach response times", "ROPA"):
            assert word in text
        assert "not required by name" in text


class TestApplicabilityConfidenceAndRationale:
    def test_every_check_states_when_it_applies(self):
        for r in assess(evidence(), TODAY).requirements:
            assert r.applicability, r.id
        assert "Applies when analytics or marketing tracking is observed" in next(
            r for r in assess(evidence(), TODAY).requirements if r.id == "DPDP-C2").applicability

    def test_confidence_is_high_for_a_direct_multi_page_observation(self):
        s = assess(evidence(trackers=[tr(1)], pages=3), TODAY)
        assert by_id(s, "DPDP-C1").confidence == "high"

    def test_confidence_is_medium_for_an_absence_a_partial_result_or_a_single_page(self):
        s = assess(evidence(trackers=[tr(1)], policies=("privacy_policy",), pages=3), TODAY)
        assert by_id(s, "DPDP-N2").confidence == "medium"            # partially met
        assert by_id(assess(evidence(policies=(), pages=3), TODAY), "DPDP-N1").confidence == "medium"   # not found within the pages
        assert by_id(assess(evidence(trackers=[tr(1)], pages=1), TODAY), "DPDP-C1").confidence == "medium"

    def test_confidence_is_none_when_nothing_was_assessed(self):
        s = assess(evidence(pages=0), TODAY)
        assert {r.confidence for r in s.requirements} == {"none"}

    def test_the_scoring_rationale_shows_the_points_and_the_weight_reason(self):
        s = assess(evidence(trackers=[tr(1)], pages=3), TODAY)
        c1, n2 = by_id(s, "DPDP-C1"), by_id(s, "DPDP-N2")
        assert c1.scoring_rationale.startswith("Not Met earns 0 of 5 points. Weight 5: Core consent obligation")
        assert n2.scoring_rationale.startswith("Partially Met earns 1 of 2 points.")

    def test_the_rationale_for_unscored_checks_says_why_they_are_not_scored(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        assert "never treated as met" in by_id(s, "DPDP-S6").scoring_rationale
        assert by_id(s, "DPDP-C2").scoring_rationale.startswith("Not scored: Counted under DPDP-C1")
