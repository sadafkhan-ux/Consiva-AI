"""The four finding groups (app/scoring/findings.py): Confirmed Findings, Needs Human Review, Unable to Verify, Future Readiness."""

from dataclasses import asdict
from datetime import date

from app.api.v1.routes.consent_scans import DpdpScoreResponse
from app.scoring import findings as fg
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import AFTER, TODAY, evidence, tr


def t(i, page="https://x.test/pricing", **kw):
    return {**tr(i, **kw), "page_url": page}


POL = ("privacy_policy", "cookie_policy")


def ids(entries):
    return [e.id for e in entries]


def entry(entries, rid):
    return next(e for e in entries if e.id == rid)


class TestClassification:
    def test_a_gap_in_a_duty_not_yet_in_force_is_future_readiness_never_confirmed(self):
        g = assess(evidence(trackers=[t(1)], pages=3), TODAY).finding_groups
        assert g.confirmed == []
        e = entry(g.future_readiness, "DPDP-C1")
        assert e.outcome == "Not Met" and e.effective_date == "2027-05-13"
        assert "Not a current violation" in e.timing_note and "2027-05-13" in e.timing_note

    def test_the_same_gap_is_a_confirmed_finding_once_the_duty_applies(self):
        g = assess(evidence(trackers=[t(1)], policies=POL, pages=3), AFTER).finding_groups
        assert ids(g.confirmed) == ["DPDP-C1"] and g.future_readiness == []
        assert entry(g.confirmed, "DPDP-C1").timing_note == "Current: the duty is in force."

    def test_the_day_before_it_applies_is_still_future_readiness(self):
        g = assess(evidence(trackers=[t(1)], policies=POL, pages=3), date(2027, 5, 12)).finding_groups
        assert g.confirmed == [] and ids(g.future_readiness) == ["DPDP-C1"]

    def test_unable_to_verify_is_its_own_group_and_never_a_gap_or_a_pass(self):
        g = assess(evidence(reject_outcome="click_failed", pages=3), TODAY).finding_groups
        e = entry(g.unable_to_verify, "DPDP-C3")
        assert e.outcome == "Unable to Verify" and e.severity is None and "neither passing nor failing" in e.severity_note
        assert "DPDP-C3" not in ids(g.confirmed) + ids(g.future_readiness)

    def test_needs_review_holds_what_a_person_must_decide(self):
        g = assess(evidence(trackers=[t(1)], pages=3), TODAY).finding_groups
        e = entry(g.needs_review, "known-tracker-before-consent")
        assert e.outcome == "Needs review" and e.severity is None and e.what_to_review and e.missing
        assert e.related_ids == ["DPDP-C1"] and "legal interpretation" in e.explanation

    def test_no_requirement_is_in_two_gap_or_unverified_groups(self):
        g = assess(evidence(trackers=[t(1)], reject=False, granular=False, policies=(), pages=5), TODAY).finding_groups
        seen = [rid for e in g.confirmed + g.future_readiness + g.unable_to_verify for rid in e.requirement_ids]
        assert len(seen) == len(set(seen))

    def test_a_suppressed_duplicate_is_not_listed_anywhere(self):
        g = assess(evidence(trackers=[t(1)], mechanism="none", pages=3), TODAY).finding_groups
        everything = g.confirmed + g.future_readiness + g.unable_to_verify
        assert "DPDP-C2" not in ids(everything)

    def test_met_and_not_applicable_checks_are_in_no_group(self):
        g = assess(evidence(pages=3), TODAY).finding_groups
        assert g.confirmed == [] and g.future_readiness == []
        assert "DPDP-C1" not in ids(g.unable_to_verify)

    def test_counts_match_the_groups(self):
        g = assess(evidence(trackers=[t(1)], pages=3), TODAY).finding_groups
        assert g.counts == {k: len(getattr(g, k)) for k in fg.GROUPS}


class TestSeverityOnlyWhereJustified:
    def test_a_direct_observation_on_a_duty_in_force_is_rated_from_its_weight(self):
        e = entry(assess(evidence(trackers=[t(1)], pages=3), AFTER).finding_groups.confirmed, "DPDP-C1")
        assert e.severity == "High" and "weight (5)" in e.severity_note

    def test_a_weight_four_check_is_medium(self):
        e = entry(assess(evidence(reject=False, pages=3), AFTER).finding_groups.confirmed, "DPDP-C4")
        assert e.severity == "Medium"

    def test_a_conclusion_that_rests_on_absence_is_not_rated(self):
        e = entry(assess(evidence(policies=(), pages=3), AFTER).finding_groups.confirmed, "DPDP-N1")
        assert e.severity is None and "not being found" in e.severity_note

    def test_a_single_page_scan_is_not_rated(self):
        e = entry(assess(evidence(trackers=[t(1)], pages=1), AFTER).finding_groups.confirmed, "DPDP-C1")
        assert e.severity is None and e.confidence == "medium"

    def test_a_partly_met_requirement_is_not_rated(self):
        e = entry(assess(evidence(reject=True, granular=False, pages=3), AFTER).finding_groups.confirmed, "DPDP-C5")
        assert e.outcome == "Partially Met" and e.severity is None and "only part" in e.severity_note

    def test_a_future_readiness_gap_is_never_rated(self):
        e = entry(assess(evidence(trackers=[t(1)], pages=3), TODAY).finding_groups.future_readiness, "DPDP-C1")
        assert e.severity is None and "not a violation" in e.severity_note

    def test_needs_review_items_are_never_rated(self):
        g = assess(evidence(trackers=[t(1)], pages=3), AFTER).finding_groups
        assert all(e.severity is None for e in g.needs_review + g.unable_to_verify)

    def test_severity_does_not_come_from_the_old_risk_levels(self):
        import inspect

        assert "risk_level" not in inspect.getsource(fg)


class TestEveryFindingIsTraceable:
    def test_a_finding_names_the_page_the_consent_state_the_evidence_the_law_the_impact_and_the_fix(self):
        e = entry(assess(evidence(trackers=[t(1)], pages=3), AFTER).finding_groups.confirmed, "DPDP-C1")
        assert e.pages == ["https://x.test/pricing"]
        assert e.consent_states == ["Before Consent"]
        assert e.evidence_ids == ["t1"] and e.rule_ids
        assert "DPDP Act, 2023 s.6(1)" in e.legal_references
        assert e.business_impact and e.remediation and e.how_to_verify and e.observed

    def test_consent_state_comes_from_the_evidence_itself(self):
        e = entry(assess(evidence(trackers=[t(1, states=("post_reject",))], pages=3), AFTER).finding_groups.confirmed, "DPDP-C3")
        assert e.consent_states == ["After Reject"]

    def test_a_request_based_finding_carries_the_caveat_that_a_request_is_not_proof(self):
        g = assess(evidence(trackers=[t(1)], policies=(), pages=3), AFTER).finding_groups
        assert "not proof of a legal violation" in entry(g.confirmed, "DPDP-C1").signal_caveat
        assert entry(g.confirmed, "DPDP-N1").signal_caveat == ""

    def test_business_impact_is_operational_not_a_legal_conclusion_or_a_penalty(self):
        for text in fg.BUSINESS_IMPACT.values():
            low = text.lower()
            assert "fine" not in low and "penalt" not in low and "crore" not in low and "violat" not in low

    def test_cookie_pages_come_from_their_sightings(self):
        c = {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": None, "source": "rule",
             "consent_states": ["pre_consent"], "observations": [{"consent_state": "pre_consent", "page_url": "https://x.test/a", "method": "x"}]}
        e = entry(assess(evidence(cookies=[c], pages=3), AFTER).finding_groups.confirmed, "DPDP-C1")
        assert e.pages == ["https://x.test/a"] and e.consent_states == ["Before Consent"]


class TestUnableToVerifyExplainsWhatIsMissing:
    def test_each_entry_says_what_is_missing_and_how_to_verify(self):
        for e in assess(evidence(pages=3), TODAY).finding_groups.unable_to_verify:
            assert e.missing and e.how_to_verify and e.explanation, e.id

    def test_business_checks_ask_for_the_organisations_evidence(self):
        e = entry(assess(evidence(pages=3), TODAY).finding_groups.unable_to_verify, "DPDP-S6")
        assert e.how_to_verify.startswith("Provide the evidence:") and "cannot show that it meets" in e.business_impact

    def test_how_to_verify_depends_on_the_cause(self):
        banner = assess(evidence(mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                                 reject_outcome="cmp_not_automatable", trackers=[tr(1, states=())], pages=3), TODAY).finding_groups
        assert "audience's region" in entry(banner.unable_to_verify, "DPDP-C3").how_to_verify
        framed = assess(evidence(framed=["https://real.test"], pages=3), TODAY).finding_groups
        assert "scan that directly" in entry(framed.unable_to_verify, "DPDP-C1").how_to_verify
        none = assess(evidence(pages=0), TODAY).finding_groups
        assert "Scan again" in entry(none.unable_to_verify, "DPDP-C1").how_to_verify
        rej = assess(evidence(reject_outcome="click_failed", pages=3), TODAY).finding_groups
        assert "Test Reject by hand" in entry(rej.unable_to_verify, "DPDP-C3").how_to_verify

    def test_the_note_makes_no_claim_about_dpia_retention_grievance_breach_or_ropa(self):
        note = assess(evidence(), TODAY).finding_groups.notes["unable_to_verify"]
        for w in ("retention", "DPIA", "grievance", "breach response times", "ROPA"):
            assert w in note

    def test_the_page_limit_is_listed_when_it_is_reached(self):
        assert "page-limit" in ids(assess(evidence(pages=5), TODAY).finding_groups.unable_to_verify)
        assert "page-limit" not in ids(assess(evidence(pages=4), TODAY).finding_groups.unable_to_verify)


class TestPartialAndEmptyScans:
    def test_with_no_pages_nothing_is_confirmed_or_a_gap(self):
        g = assess(evidence(pages=0), AFTER).finding_groups
        assert g.confirmed == [] and g.future_readiness == []
        assert len(g.unable_to_verify) >= 5

    def test_a_framed_wrapper_confirms_only_what_it_observed(self):
        g = assess(evidence(framed=["https://real.test"], trackers=[t(1)], policies=(), pages=3, mechanism="none"), AFTER).finding_groups
        assert [e.id for e in g.confirmed] == ["DPDP-C1"] and g.future_readiness == []     # the tracker seen; nothing from an absence

    def test_a_clean_framed_wrapper_confirms_nothing(self):
        g = assess(evidence(framed=["https://real.test"], trackers=[], policies=(), pages=3, mechanism="none"), AFTER).finding_groups
        assert g.confirmed == [] and g.future_readiness == []


class TestUpcomingDates:
    def test_every_applicable_future_duty_is_listed_by_its_start_date(self):
        g = assess(evidence(pages=3), TODAY).finding_groups
        assert g.upcoming[0].date == "2027-05-13"
        assert "DPDP-C1" in g.upcoming[0].requirement_ids and "DPDP-X10" not in g.upcoming[0].requirement_ids

    def test_nothing_is_upcoming_once_everything_applies(self):
        assert assess(evidence(pages=3), AFTER).finding_groups.upcoming == []

    def test_a_declared_consent_manager_duty_starts_a_year_after_publication(self):
        from app.scoring.dpdp_score import Applicability

        g = assess(evidence(pages=3), TODAY, Applicability(consent_manager=True)).finding_groups
        assert "2026-11-13" in [u.date for u in g.upcoming]


def test_the_whole_structure_serialises_through_the_api_model():
    m = DpdpScoreResponse(**asdict(assess(evidence(trackers=[t(1)], pages=5), TODAY)))
    assert m.finding_groups.future_readiness and m.finding_groups.needs_review and m.finding_groups.unable_to_verify
