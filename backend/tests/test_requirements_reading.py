"""The DPDP requirements reading (app/scoring/requirements.py): status rules, exclusions, duplicate suppression, effective dates and traceability. There is no score
here: the one score is the ten-check assessment (tests/test_ten_checks_parity.py). Pure functions, no database."""

from datetime import date

from app.rules.consent_rules import evaluate_consent_rules
from app.scoring import requirements as ds
from app.scoring.requirements import Applicability, assess

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


class TestStatuses:
    def test_worked_example_clean_site(self):
        s = assess(evidence(), TODAY)
        assert [by_id(s, r).status for r in ("DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5", "DPDP-N1", "DPDP-N2")] == [
            "met", "not_applicable", "met", "met", "met", "met", "not_applicable"]
        assert {r.id for r in s.requirements if r.status == "unable_to_verify"} == {"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"}

    def test_worked_example_mixed_site(self):
        s = assess(evidence(trackers=[tr(1)], reject=False, granular=False), TODAY)
        assert [by_id(s, r).status for r in ("DPDP-C1", "DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5", "DPDP-N1", "DPDP-N2")] == [
            "not_met", "met", "met", "not_met", "not_applicable", "met", "partially_met"]

    def test_the_result_carries_no_number_about_a_score(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        banned = ("weight", "points", "percent", "coverage", "score", "counts_in_score", "penalty")
        for r in s.requirements:
            assert not [k for k in vars(r) if any(b in k for b in banned)], r.id
        assert not [k for k in vars(s) if any(b in k for b in ("readiness", "in_force_block", "thresholds", "deductions", "score"))]


class TestUnableToVerify:
    def test_unable_to_verify_is_reported_separately(self):
        s = assess(evidence(), TODAY)
        utv = [r for r in s.requirements if r.status == "unable_to_verify"]
        assert {r.id for r in utv} == {"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"}
        assert s.ledger.unable_checks == len(utv)

    def test_untested_reject_is_never_met(self):
        s = assess(evidence(reject_outcome="cmp_not_automatable"), TODAY)
        assert by_id(s, "DPDP-C3").status == "unable_to_verify"

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
        assert sum(1 for r in s.requirements if r.status in ("met", "partially_met", "not_met")) == 1   # one assessed requirement

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


class TestNothingAssessed:
    def test_no_pages_assesses_nothing_as_met(self):
        s = assess(evidence(pages=0), TODAY)
        assert not any(r.status == "met" for r in s.requirements)
        assert s.ledger.passed_checks == 0


class TestApplicabilityAndExclusions:
    def test_entity_specific_requirements_are_excluded_unless_declared(self):
        s = assess(evidence(), TODAY)
        for rid in ("DPDP-X4", "DPDP-X8", "DPDP-X10", "DPDP-X13"):
            r = by_id(s, rid)
            assert r.status == "not_applicable" and r.exclusion_reason

    def test_declaring_applicability_adds_them_as_unable_to_verify_and_lowers_coverage(self):
        base = assess(evidence(), TODAY)
        s = assess(evidence(), TODAY, Applicability(processes_children_data=True, significant_data_fiduciary=True))
        assert by_id(s, "DPDP-X10").status == "unable_to_verify" and by_id(s, "DPDP-X13").status == "unable_to_verify"
        assert s.ledger.applicable_checks == base.ledger.applicable_checks + 2
        assert s.ledger.unable_checks == base.ledger.unable_checks + 2

    def test_declared_not_applicable_is_distinguished_from_undeclared(self):
        s = assess(evidence(), TODAY, Applicability(processes_children_data=False))
        assert "Declared as not applicable" in by_id(s, "DPDP-X10").exclusion_reason
        assert "excluded unless declared" in by_id(s, "DPDP-X13").exclusion_reason

    def test_no_tracking_makes_consent_mechanism_and_disclosure_not_applicable(self):
        s = assess(evidence(), TODAY)
        assert by_id(s, "DPDP-C2").status == "not_applicable" and by_id(s, "DPDP-N2").status == "not_applicable"

    def test_no_tracking_is_not_applicable_only_when_accept_could_have_shown_it(self):
        """A consent tool is present and Accept was clicked: nothing fired after consent either, so tracking is not used. Accept NOT clicked: nothing after consent was ever seen,
        so the absence is untested and the two checks are Unable to Verify, not Not Applicable (uat.consiva.ai: one failed Accept click changed which checks applied)."""
        tested = assess(evidence(accept_outcome="clicked"), TODAY)
        assert by_id(tested, "DPDP-C2").status == "not_applicable" and by_id(tested, "DPDP-N2").status == "not_applicable"
        for outcome in ("cmp_not_automatable", "click_failed"):
            s = assess(evidence(accept_outcome=outcome), TODAY)
            for rid in ("DPDP-C2", "DPDP-N2"):
                r = by_id(s, rid)
                assert r.status == "unable_to_verify", (outcome, rid)
                assert "Accept choice could not be tested" in " ".join(r.observations)
        # the same two checks are never passed or failed while Accept is untested: they stay open
        a, b = assess(evidence(accept_outcome="clicked"), TODAY), assess(evidence(accept_outcome="cmp_not_automatable"), TODAY)
        assert b.ledger.unable_checks == a.ledger.unable_checks + 2

    def test_without_a_consent_tool_or_on_an_older_scan_an_absence_is_still_not_applicable(self):
        for kw in ({"mechanism": "none", "accept_outcome": "cmp_not_found"}, {"three_pass": False}):
            s = assess(evidence(**kw), TODAY)
            assert by_id(s, "DPDP-C2").status == "not_applicable" and by_id(s, "DPDP-N2").status == "not_applicable", kw

    def test_tracking_that_was_seen_is_scored_whatever_the_accept_outcome(self):
        s = assess(evidence(trackers=[tr(1, states=("post_reject",))], accept_outcome="cmp_not_automatable"), TODAY)
        assert by_id(s, "DPDP-C2").status != "not_applicable" and by_id(s, "DPDP-N2").status != "not_applicable"     # tracking was observed, so both checks apply

    def test_no_banner_makes_banner_choices_not_applicable_rather_than_failed(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        for rid in ("DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id(s, rid).status == "not_applicable", rid


class TestDuplicatePenalties:
    def test_tracking_with_no_banner_is_one_gap_not_two(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        c1, c2 = by_id(s, "DPDP-C1"), by_id(s, "DPDP-C2")
        assert c1.status == "not_met" and c2.status == "not_applicable"
        assert c2.duplicate_of == "DPDP-C1" and "Listed under DPDP-C1" in c2.exclusion_reason

    def test_banner_without_reject_or_granular_choices_is_one_gap(self):
        s = assess(evidence(reject=False, granular=False), TODAY)
        assert by_id(s, "DPDP-C4").status == "not_met" and by_id(s, "DPDP-C5").status == "not_applicable"

    def test_no_notice_at_all_is_one_gap(self):
        s = assess(evidence(trackers=[tr(1, states=())], policies=()), TODAY)
        assert by_id(s, "DPDP-N1").status == "not_met" and by_id(s, "DPDP-N2").status == "not_applicable"

    def test_the_first_listed_keeps_the_gap(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none"), TODAY)
        assert by_id(s, "DPDP-C1").duplicate_of is None and by_id(s, "DPDP-C2").duplicate_of == "DPDP-C1"

    def test_a_single_failure_in_a_group_is_not_suppressed(self):
        s = assess(evidence(reject=False, granular=True), TODAY)
        assert by_id(s, "DPDP-C4").status == "not_met" and by_id(s, "DPDP-C5").status == "met"


class TestEffectiveDates:
    def test_consent_and_notice_duties_are_not_yet_in_force_in_october_2026(self):
        s = assess(evidence(), TODAY)
        assert not any(r.in_force for r in s.requirements if r.id.startswith(("DPDP-C", "DPDP-N")))

    def test_in_force_on_the_eighteen_month_date(self):
        s = assess(evidence(), AFTER)
        assert all(r.in_force for r in s.requirements)

    def test_the_day_before_is_not_in_force(self):
        assert not by_id(assess(evidence(), date(2027, 5, 12)), "DPDP-C1").in_force

    def test_consent_manager_rule_starts_after_one_year(self):
        r = by_id(assess(evidence(), date(2026, 11, 12), Applicability(consent_manager=True)), "DPDP-X4")
        assert not r.in_force
        assert by_id(assess(evidence(), date(2026, 11, 13), Applicability(consent_manager=True)), "DPDP-X4").in_force


class TestTraceability:
    def test_every_requirement_cites_a_provision_and_a_severity(self):
        for req in ds.REQUIREMENTS:
            assert req.provisions and all(p.startswith(("DPDP Act, 2023 s.", "DPDP Rules, 2025 r.")) for p in req.provisions), req.id
            assert req.severity in ("High", "Medium", "Low")

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

    def test_registry_ids_are_unique(self):
        ids = [r.id for r in ds.REQUIREMENTS]
        assert len(ids) == len(set(ids)) == 16

    def test_the_reading_is_versioned_and_carries_the_disclaimer(self):
        s = assess(evidence(), TODAY)
        assert s.requirements_version == ds.REQUIREMENTS_VERSION
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

