"""DPDP Compliance Score (app/scoring/dpdp_score.py, docs/DPDP_COMPLIANCE_SCORE.md): formula, status rules, coverage, exclusions,
duplicate suppression, effective dates, and the worked examples the documentation prints. Pure functions, no database."""

from datetime import date

from app.rules.consent_rules import evaluate_consent_rules
from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import Applicability, assess

TODAY = date(2026, 10, 21)           # before the 18-month date: consent and notice duties are not yet in force
AFTER = date(2027, 5, 13)            # the day they come into force


def evidence(*, trackers=(), cookies=(), policies=("privacy_policy",), pages=1, mechanism="banner", reject=True, granular=True,
             accept_outcome="clicked", reject_outcome="clicked", confirmed=True, displayed=True, framed=(), three_pass=True):
    ev = {"accept_interaction": accept_outcome, "reject_interaction": reject_outcome, "reject_click_confirmed": confirmed,
          "banner_ui_observed": displayed}
    if not three_pass:
        ev = {}
    if framed:
        ev["framed_sites"] = list(framed)
    signals = [] if mechanism == "unknown" else [{
        "mechanism_type": mechanism, "cmp_vendor": "OneTrust" if mechanism == "cmp" else None,
        "has_reject_all": reject if mechanism in ("banner", "cmp") else None,
        "has_granular_choices": granular if mechanism in ("banner", "cmp") else None, "evidence": ev,
    }]
    return {
        "pages": [{"id": f"p{i}", "url": f"https://x.test/{i}", "title": None} for i in range(pages)],
        "forms": [], "third_party_services": [], "consent_signals": signals,
        "policies": [{"id": f"pol{i}", "url": "https://x.test/p", "policy_type": t} for i, t in enumerate(policies)],
        "cookies": list(cookies), "trackers": list(trackers),
    }


def tr(i, category="analytics", states=("pre_consent",)):
    return {"id": f"t{i}", "script_src": f"https://t{i}.test/a.js", "vendor": None, "category": category,
            "source": "rule", "consent_states": list(states)}


def by_id(score, rid):
    return next(r for r in score.requirements if r.id == rid)


class TestFormula:
    def test_worked_example_clean_site(self):
        """Banner with reject and granular choices, a privacy policy, no tracking. C1 5 + C3 4 + C4 4 + C5 2 + N1 4 = 19 earned
        of 19 assessed = 100.0%. Applicable = 19 + 14 (five requirements a scan cannot see) = 33, so coverage = 19/33 = 57.6%."""
        s = assess(evidence(), TODAY).readiness
        assert (s.points_earned, s.points_assessed) == (19, 19)
        assert s.score_percent == 100.0
        assert (s.assessed_weight, s.applicable_weight, s.unable_to_verify_weight) == (19, 33, 14)
        assert s.coverage_percent == 57.6
        assert s.status == "scored"

    def test_worked_example_mixed_site(self):
        """Tracker fires before consent; reject-all and granular choices are both missing; privacy policy found, no cookie policy.
        C1 Not Met 0/5, C2 Met 4/4, C3 Met 4/4, C4 Not Met 0/4, C5 suppressed (same gap as C4), N1 Met 4/4, N2 Partially Met 1/2.
        Earned 13, assessed 23 -> 56.5%. Applicable 37 -> coverage 62.2%."""
        s = assess(evidence(trackers=[tr(1)], reject=False, granular=False), TODAY)
        assert [by_id(s, r).status for r in ("DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5", "DPDP-N1", "DPDP-N2")] == [
            "not_met", "met", "met", "not_met", "not_applicable", "met", "partially_met"]
        assert (s.readiness.points_earned, s.readiness.points_assessed) == (13, 23)
        assert s.readiness.score_percent == 56.5
        assert s.readiness.applicable_weight == 37
        assert s.readiness.coverage_percent == 62.2

    def test_partially_met_is_exactly_half_of_the_weight(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        n2 = by_id(s, "DPDP-N2")
        assert n2.status == "partially_met" and n2.weight == 2 and n2.points_earned == 1.0

    def test_not_met_earns_nothing_and_counts_in_the_denominator(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        c1 = by_id(s, "DPDP-C1")
        assert c1.status == "not_met" and c1.points_earned == 0 and c1.counts_in_score

    def test_rounding_is_half_up_to_one_decimal(self):
        assert ds._pct(1, 8) == 12.5
        assert ds._pct(1, 3) == 33.3
        assert ds._pct(2, 3) == 66.7
        assert ds._pct(5, 8000) == 0.1          # 0.0625 rounds up
        assert ds._pct(0, 5) == 0.0

    def test_exact_fractions_not_floats(self):
        # 2.5 points of 7 is 35.714...% -> 35.7, with no float drift
        assert ds._pct(__import__("fractions").Fraction(5, 2), 7) == 35.7

    def test_score_never_exceeds_100_or_goes_below_0(self):
        for kw in (dict(), dict(trackers=[tr(1)], mechanism="none", policies=()), dict(trackers=[tr(1)], reject=False)):
            s = assess(evidence(**kw), TODAY).readiness
            if s.score_percent is not None:
                assert 0.0 <= s.score_percent <= 100.0


class TestUnableToVerify:
    def test_excluded_from_denominator_and_reported_separately(self):
        s = assess(evidence(), TODAY)
        utv = [r for r in s.requirements if r.status == "unable_to_verify"]
        assert {r.id for r in utv} == {"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"}
        assert all(not r.counts_in_score for r in utv)
        assert s.readiness.unable_to_verify_weight == sum(r.weight for r in utv) == 14

    def test_untested_reject_is_never_met(self):
        s = assess(evidence(reject_outcome="cmp_not_automatable"), TODAY)
        c3 = by_id(s, "DPDP-C3")
        assert c3.status == "unable_to_verify" and c3.points_earned == 0

    def test_unconfirmed_reject_click_is_unable_to_verify(self):
        assert by_id(assess(evidence(confirmed=False), TODAY), "DPDP-C3").status == "unable_to_verify"

    def test_banner_not_displayed_makes_banner_requirements_unverifiable(self):
        s = assess(evidence(mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                            reject_outcome="cmp_not_automatable", trackers=[tr(1)]), TODAY)
        for rid in ("DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id(s, rid).status == "unable_to_verify", rid

    def test_scan_predating_three_state_testing_cannot_claim_no_pre_consent_tracking(self):
        s = assess(evidence(three_pass=False), TODAY)
        assert by_id(s, "DPDP-C1").status == "unable_to_verify"

    def test_a_framed_wrapper_is_limited_only_where_the_answer_rests_on_nothing_being_seen(self):
        """Requirement by requirement (audit 2026-10-26): what the scan SAW stands, what it did not see proves nothing about the framed site."""
        s = assess(evidence(framed=["https://real.test"], trackers=[tr(1)], policies=(), mechanism="none"), TODAY)
        # A tracker that fired while the address loaded fired: that observation is kept, and says where it was seen.
        assert by_id(s, "DPDP-C1").status == "not_met" and "wrapper" in by_id(s, "DPDP-C1").observations[0]
        # No banner / no policy / no clean Reject seen on a wrapper is not a finding about the framed site.
        for rid in ("DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-N1", "DPDP-N2"):
            assert by_id(s, rid).status in ("unable_to_verify", "not_applicable"), rid
        assert by_id(s, "DPDP-N1").status == "unable_to_verify" and "frame" in by_id(s, "DPDP-N1").observations[0]
        assert s.readiness.status == "insufficient_evidence"          # one assessed requirement is not enough for a number

    def test_a_clean_framed_wrapper_is_never_a_pass(self):
        s = assess(evidence(framed=["https://real.test"], trackers=[], policies=(), mechanism="none"), TODAY)
        assert not any(r.status == "met" for r in s.requirements)
        assert by_id(s, "DPDP-C1").status == "unable_to_verify" and by_id(s, "DPDP-N1").status == "unable_to_verify"

    def test_a_policy_link_found_on_a_wrapper_is_kept_with_where_it_was_seen(self):
        s = assess(evidence(framed=["https://real.test"], trackers=[], policies=("privacy_policy",), mechanism="none"), TODAY)
        n1 = by_id(s, "DPDP-N1")
        assert n1.status == "met" and "wrapper" in n1.observations[0]

    def test_the_same_checks_without_a_frame_are_unchanged(self):
        s = assess(evidence(trackers=[tr(1)], policies=(), mechanism="none"), TODAY)
        assert by_id(s, "DPDP-C1").status == "not_met" and by_id(s, "DPDP-N1").status == "not_met"
        assert by_id(s, "DPDP-C2").status == "not_applicable"      # its gap is carried by DPDP-C1 (duplicate suppression), never silently passed

    def test_missing_evidence_is_never_compliance(self):
        """With no pages scanned nothing is Met, nothing is scored."""
        s = assess(evidence(pages=0), TODAY)
        assert not any(r.status == "met" for r in s.requirements)
        assert s.readiness.score_percent is None and s.readiness.status == "insufficient_evidence"


class TestInsufficientEvidence:
    def test_no_pages(self):
        s = assess(evidence(pages=0), TODAY).readiness
        assert s.status == "insufficient_evidence" and s.score_percent is None
        assert "could be assessed" in s.insufficient_reason

    def test_below_minimum_coverage(self):
        # banner not displayed: only C1 (5) and N1 (4) can be assessed -> 9 of 37 = 24.3% < 30%
        s = assess(evidence(mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                            reject_outcome="cmp_not_automatable", trackers=[tr(1, states=())]), TODAY).readiness
        assert s.coverage_percent is not None and s.coverage_percent < ds.MIN_COVERAGE_PERCENT
        assert s.status == "insufficient_evidence" and s.score_percent is None
        assert "coverage" in s.insufficient_reason

    def test_coverage_is_still_reported_when_the_score_is_withheld(self):
        s = assess(evidence(pages=0), TODAY).readiness
        assert s.coverage_percent == 0.0 and s.assessed_weight == 0 and s.applicable_weight == 33 - 0

    def test_a_clearly_failing_site_is_scored_not_withheld(self):
        # no banner, tracking before consent, no policy: C1 and N1 Not Met; C2 and N2 are suppressed duplicates
        s = assess(evidence(trackers=[tr(1)], mechanism="none", policies=()), TODAY)
        assert s.readiness.status == "scored" and s.readiness.score_percent == 0.0


class TestApplicabilityAndExclusions:
    def test_entity_specific_requirements_are_excluded_unless_declared(self):
        s = assess(evidence(), TODAY)
        for rid in ("DPDP-X4", "DPDP-X8", "DPDP-X10", "DPDP-X13"):
            r = by_id(s, rid)
            assert r.status == "not_applicable" and not r.counts_in_score and r.exclusion_reason
        assert s.readiness.applicable_weight == 33

    def test_declaring_applicability_adds_them_as_unable_to_verify_and_lowers_coverage(self):
        base = assess(evidence(), TODAY).readiness
        s = assess(evidence(), TODAY, Applicability(processes_children_data=True, significant_data_fiduciary=True))
        assert by_id(s, "DPDP-X10").status == "unable_to_verify" and by_id(s, "DPDP-X13").status == "unable_to_verify"
        assert s.readiness.applicable_weight == base.applicable_weight + 6
        assert s.readiness.score_percent == base.score_percent            # never moves the score
        assert s.readiness.coverage_percent < base.coverage_percent

    def test_declared_not_applicable_is_distinguished_from_undeclared(self):
        s = assess(evidence(), TODAY, Applicability(processes_children_data=False))
        assert "Declared as not applicable" in by_id(s, "DPDP-X10").exclusion_reason
        assert "excluded unless declared" in by_id(s, "DPDP-X13").exclusion_reason

    def test_no_tracking_makes_consent_mechanism_and_disclosure_not_applicable(self):
        s = assess(evidence(), TODAY)
        assert by_id(s, "DPDP-C2").status == "not_applicable" and by_id(s, "DPDP-N2").status == "not_applicable"

    def test_no_banner_makes_banner_choices_not_applicable_rather_than_failed(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        for rid in ("DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id(s, rid).status == "not_applicable", rid


class TestDuplicatePenalties:
    def test_tracking_with_no_banner_is_one_gap_not_two(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        c1, c2 = by_id(s, "DPDP-C1"), by_id(s, "DPDP-C2")
        assert c1.status == "not_met" and c2.status == "not_applicable"
        assert not c2.counts_in_score and c2.duplicate_of == "DPDP-C1" and "Counted under DPDP-C1" in c2.exclusion_reason

    def test_suppressed_requirement_leaves_numerator_and_denominator(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY).readiness
        # C1 (5, Not Met) + N2 (2, Partially Met 1) + N1 (4, Met 4) = 5 + 4 + 2 assessed; C2 suppressed
        assert s.points_assessed == 11 and s.points_earned == 5

    def test_banner_without_reject_or_granular_choices_is_one_gap(self):
        s = assess(evidence(reject=False, granular=False), TODAY)
        assert by_id(s, "DPDP-C4").status == "not_met" and by_id(s, "DPDP-C5").status == "not_applicable"

    def test_no_notice_at_all_is_one_gap(self):
        s = assess(evidence(trackers=[tr(1, states=())], policies=()), TODAY)
        assert by_id(s, "DPDP-N1").status == "not_met" and by_id(s, "DPDP-N2").status == "not_applicable"

    def test_heaviest_keeps_the_penalty(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        assert by_id(s, "DPDP-C1").weight > by_id(s, "DPDP-C2").weight

    def test_a_single_failure_in_a_group_is_not_suppressed(self):
        s = assess(evidence(reject=False, granular=True), TODAY)
        assert by_id(s, "DPDP-C4").status == "not_met" and by_id(s, "DPDP-C5").status == "met"


class TestEffectiveDates:
    def test_consent_and_notice_duties_are_not_yet_in_force_in_october_2026(self):
        s = assess(evidence(), TODAY)
        assert not any(r.in_force for r in s.requirements if r.id.startswith(("DPDP-C", "DPDP-N")))
        assert s.in_force.status == "insufficient_evidence"
        assert s.in_force.applicable_weight == 0 and s.in_force.score_percent is None
        assert "in force" in s.in_force.insufficient_reason

    def test_readiness_score_is_available_before_they_apply(self):
        assert assess(evidence(), TODAY).readiness.status == "scored"

    def test_in_force_on_the_eighteen_month_date(self):
        s = assess(evidence(), AFTER)
        assert all(r.in_force for r in s.requirements)
        assert s.in_force.status == "scored" and s.in_force.score_percent == s.readiness.score_percent == 100.0

    def test_the_day_before_is_not_in_force(self):
        assert not by_id(assess(evidence(), date(2027, 5, 12)), "DPDP-C1").in_force

    def test_consent_manager_rule_starts_after_one_year(self):
        r = by_id(assess(evidence(), date(2026, 11, 12), Applicability(consent_manager=True)), "DPDP-X4")
        assert not r.in_force
        assert by_id(assess(evidence(), date(2026, 11, 13), Applicability(consent_manager=True)), "DPDP-X4").in_force


class TestCategories:
    def test_category_scores_use_the_same_formula(self):
        s = assess(evidence(trackers=[tr(1)], reject=False, granular=False), TODAY).readiness
        cats = {c["category"]: c for c in s.categories}
        assert cats["Consent"]["points_earned"] == 8 and cats["Consent"]["assessed_weight"] == 17
        assert cats["Consent"]["score_percent"] == 47.1
        assert cats["Notice"]["score_percent"] == 83.3
        assert cats["Data principal rights"]["status"] == "not_assessed" and cats["Data principal rights"]["score_percent"] is None

    def test_category_weights_add_up_to_the_overall(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY).readiness
        assert sum(c["assessed_weight"] for c in s.categories) == s.assessed_weight
        assert sum(c["applicable_weight"] for c in s.categories) == s.applicable_weight
        assert sum(c["points_earned"] for c in s.categories) == s.points_earned


class TestTraceability:
    def test_every_requirement_cites_a_provision_and_a_weight_rationale(self):
        for req in ds.REQUIREMENTS:
            assert req.provisions and all(p.startswith(("DPDP Act, 2023 s.", "DPDP Rules, 2025 r.")) for p in req.provisions), req.id
            assert req.weight_rationale and req.weight > 0

    def test_results_carry_evidence_ids_and_the_rule_that_flags_the_same_thing(self):
        s = assess(evidence(trackers=[tr(1), tr(2)]), TODAY)
        c1 = by_id(s, "DPDP-C1")
        assert c1.evidence_ids == ["t1", "t2"] and "R-001" in c1.rule_ids and c1.observations

    def test_a_requirement_is_not_met_exactly_when_the_matching_rule_fires(self):
        """The score and the findings read the same context, so they cannot disagree about the same fact."""
        cases = [
            (evidence(trackers=[tr(1)]), "DPDP-C1", "R-001"),
            (evidence(trackers=[tr(1)], mechanism="none"), "DPDP-C2", "R-002"),
            (evidence(reject=False), "DPDP-C4", "R-005"),
            (evidence(policies=()), "DPDP-N1", "R-006"),
            (evidence(trackers=[tr(1, states=())], policies=("privacy_policy",)), "DPDP-N2", "R-007"),
        ]
        for ev, rid, rule in cases:
            fired = rule in {f.rule_id for f in evaluate_consent_rules(ev)}
            status = by_id(assess(ev, TODAY), rid).status
            assert fired, (rid, rule)
            assert status in ("not_met", "partially_met", "not_applicable"), (rid, status)

    def test_reject_honoured_not_met_matches_r003(self):
        ev = evidence(trackers=[tr(1, states=("post_reject",))])
        assert "R-003" in {f.rule_id for f in evaluate_consent_rules(ev)}
        assert by_id(assess(ev, TODAY), "DPDP-C3").status == "not_met"

    def test_registry_ids_are_unique_and_weights_documented(self):
        ids = [r.id for r in ds.REQUIREMENTS]
        assert len(ids) == len(set(ids))
        assert sum(r.weight for r in ds.REQUIREMENTS if r.applies_if is None) == 39    # every universal requirement applicable

    def test_methodology_is_versioned_and_labelled_as_proposed(self):
        s = assess(evidence(), TODAY)
        assert s.methodology_version == ds.METHODOLOGY_VERSION == "1.5.0"
        assert "for informational purposes only" in s.disclaimer and "does not constitute legal advice" in s.disclaimer


class TestDeterminism:
    def test_same_evidence_same_date_same_result(self):
        a, b = assess(evidence(trackers=[tr(1)]), TODAY), assess(evidence(trackers=[tr(1)]), TODAY)
        assert a == b

    def test_evidence_is_not_mutated(self):
        ev = evidence(trackers=[tr(1)])
        import copy
        before = copy.deepcopy(ev)
        assess(ev, TODAY)
        assert ev == before

