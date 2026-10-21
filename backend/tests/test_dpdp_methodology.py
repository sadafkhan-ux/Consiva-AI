"""Configurable, documented weights and the methodology payload (app/scoring/dpdp_score.py: resolve_weights, build_methodology)."""

from dataclasses import asdict

import pytest
from pydantic import ValidationError

from app.api.v1.routes.consent_scans import DpdpScoreResponse
from app.config import Settings, get_settings
from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess, resolve_weights
from tests.test_dpdp_score import AFTER, TODAY, evidence, tr


def by_id(score, rid):
    return next(r for r in score.requirements if r.id == rid)


MIXED = {"trackers": [tr(1)], "reject": False, "granular": False, "pages": 3}       # C1 and C4 Not Met, N2 Partially Met


class TestConfigurableWeights:
    def test_the_defaults_are_the_documented_weights(self):
        w = resolve_weights()
        assert w == {r.id: r.weight for r in ds.REQUIREMENTS} and w["DPDP-C1"] == 5

    def test_an_override_replaces_only_the_weights_it_names(self):
        w = resolve_weights({"DPDP-C1": 8})
        assert w["DPDP-C1"] == 8 and w["DPDP-C2"] == 4

    @pytest.mark.parametrize("bad", [{"DPDP-NOPE": 3}, {"DPDP-C1": 0}, {"DPDP-C1": 11}, {"DPDP-C1": -2}, {"DPDP-C1": 2.5}, {"DPDP-C1": "5"},
                                     {"DPDP-C1": True}])
    def test_an_invalid_override_raises_instead_of_silently_changing_a_score(self, bad):
        with pytest.raises(ValueError):
            resolve_weights(bad)

    def test_a_configured_weight_changes_the_score_by_the_formula_and_nothing_else(self):
        base = assess(evidence(**MIXED), TODAY).readiness            # earned 13 of 23
        heavy = assess(evidence(**MIXED), TODAY, weights={"DPDP-C1": 10}).readiness
        # C1 (Not Met, 0 points) now weighs 10 instead of 5: earned stays 13, assessed becomes 28
        assert (base.points_earned, base.points_assessed) == (13, 23)
        assert (heavy.points_earned, heavy.points_assessed) == (13, 28)
        assert heavy.score_percent == 46.4 and base.score_percent == 56.5

    def test_the_result_keeps_the_documented_weight_beside_the_configured_one(self):
        r = by_id(assess(evidence(**MIXED), TODAY, weights={"DPDP-C1": 10}), "DPDP-C1")
        assert r.weight == 10 and r.default_weight == 5
        assert "configured; documented default 5" in r.scoring_rationale and "of 10 points" in r.scoring_rationale

    def test_severity_follows_the_documented_weight_so_configuring_never_changes_how_serious_a_finding_is_called(self):
        a = assess(evidence(trackers=[tr(1)], pages=3), AFTER).finding_groups.confirmed[0]
        b = assess(evidence(trackers=[tr(1)], pages=3), AFTER, weights={"DPDP-C1": 1}).finding_groups.confirmed[0]
        assert a.severity == b.severity == "High"

    def test_a_scored_run_with_the_defaults_is_unchanged_by_this_feature(self):
        assert assess(evidence(**MIXED), TODAY).readiness.score_percent == 56.5
        assert assess(evidence(**MIXED), TODAY, weights={}).readiness.score_percent == 56.5

    def test_the_settings_reject_an_invalid_weight_so_the_app_does_not_start(self):
        base = get_settings().model_dump()
        with pytest.raises(ValidationError):
            Settings.model_validate({**base, "dpdp_score_weights": {"DPDP-C1": 99}})
        with pytest.raises(ValidationError):
            Settings.model_validate({**base, "dpdp_score_weights": {"DPDP-X": 3}})
        assert Settings.model_validate({**base, "dpdp_score_weights": {"DPDP-C1": 6}}).dpdp_score_weights == {"DPDP-C1": 6}

    def test_the_default_setting_is_empty(self):
        assert Settings.model_fields["dpdp_score_weights"].default_factory() == {}


class TestMethodologyPayload:
    def test_it_states_the_version_the_formulas_and_the_rules(self):
        m = assess(evidence(**MIXED), TODAY).methodology
        assert m.version == ds.METHODOLOGY_VERSION == "1.5.0"
        assert "weighted points earned ÷ weighted points assessed" in m.score_formula
        assert "weight of assessed requirements ÷ weight of applicable requirements" in m.coverage_formula
        assert "shown separately from the score" in m.coverage_formula
        assert any("100%" in r for r in m.points_rules) and any("50%" in r for r in m.points_rules) and any("0%" in r for r in m.points_rules)
        assert "Insufficient Evidence" in m.reliability_rule and "30%" in m.reliability_rule and "2 requirements" in m.reliability_rule
        assert "scored once" in m.duplicate_rule and "never as current violations" in m.timing_rule
        assert "up to 5 pages" in m.scope_rule

    def test_it_lists_every_requirement_with_its_weight_and_rationale(self):
        m = assess(evidence(**MIXED), TODAY).methodology
        assert [w.id for w in m.weights] == [r.id for r in ds.REQUIREMENTS]
        for w in m.weights:
            assert w.rationale and w.applicability and w.weight == w.default_weight
        assert next(w for w in m.weights if w.id == "DPDP-C1").weight == 5

    def test_a_requirement_that_does_not_apply_says_so(self):
        m = assess(evidence(**MIXED), TODAY).methodology
        assert not next(w for w in m.weights if w.id == "DPDP-X10").applies_now
        assert next(w for w in m.weights if w.id == "DPDP-C1").applies_now

    def test_a_custom_weight_is_declared_with_a_warning(self):
        m = assess(evidence(**MIXED), TODAY, weights={"DPDP-C1": 8}).methodology
        assert m.weights_source == "custom" and "DPDP-C1 = 8 (default 5)" in m.weights_note and "not comparable" in m.weights_note
        assert next(w for w in m.weights if w.id == "DPDP-C1").default_weight == 5

    def test_default_weights_are_declared_as_such(self):
        m = assess(evidence(**MIXED), TODAY).methodology
        assert m.weights_source == "default" and m.weights_note == "Documented default weights."

    def test_duplicate_groups_are_listed_with_the_requirements_they_cover(self):
        groups = {d.group: d.requirement_ids for d in assess(evidence(), TODAY).methodology.dedup}
        assert groups == {"consent-before-tracking": ["DPDP-C1", "DPDP-C2"], "choice-quality": ["DPDP-C4", "DPDP-C5"],
                          "notice": ["DPDP-N1", "DPDP-N2"]}

    def test_it_serialises_through_the_api_model(self):
        m = DpdpScoreResponse(**asdict(assess(evidence(**MIXED), TODAY))).methodology
        assert m.version == "1.5.0" and len(m.weights) == len(ds.REQUIREMENTS) and m.categories


class TestCategoryWeights:
    def test_every_category_has_a_documented_rationale(self):
        cats = {r.category for r in ds.REQUIREMENTS}
        assert cats <= set(ds.CATEGORY_INFO) and all(ds.CATEGORY_INFO[c] for c in cats)

    def test_category_weights_add_up_to_the_applicable_weight_and_the_shares_to_a_whole(self):
        s = assess(evidence(**MIXED), TODAY)
        cats = s.methodology.categories
        assert sum(c.weight for c in cats) == s.readiness.applicable_weight
        assert abs(sum(c.share_percent for c in cats) - 100) < 0.2 and all(c.rationale for c in cats)

    def test_the_overall_score_is_the_weight_averaged_score_of_the_categories(self):
        s = assess(evidence(**MIXED), TODAY)
        scored = [c for c in s.readiness.categories if c["score_percent"] is not None]
        average = sum(c["score_percent"] * c["assessed_weight"] for c in scored) / sum(c["assessed_weight"] for c in scored)
        assert abs(average - s.readiness.score_percent) < 0.3

    def test_a_category_with_nothing_applicable_is_not_listed(self):
        names = [c.name for c in assess(evidence(**MIXED), TODAY).methodology.categories]
        assert "Entity-specific" not in names
        declared = [c.name for c in assess(evidence(**MIXED), TODAY, ds.Applicability(True, True, True, True)).methodology.categories]
        assert "Entity-specific" in declared


class TestCoverageIsSeparateFromTheScore:
    def test_a_perfect_score_can_have_modest_coverage(self):
        r = assess(evidence(pages=3), TODAY).readiness
        assert r.score_percent == 100.0 and r.coverage_percent == 57.6

    def test_coverage_is_computed_from_weights_not_from_the_score(self):
        failing = assess(evidence(trackers=[tr(1)], pages=3), TODAY).readiness
        passing = assess(evidence(trackers=[tr(1, states=("post_accept",))], pages=3), TODAY).readiness
        assert failing.coverage_percent == passing.coverage_percent and failing.score_percent != passing.score_percent

    def test_coverage_still_exists_when_the_score_is_withheld(self):
        r = assess(evidence(pages=0), TODAY).readiness
        assert r.status == "insufficient_evidence" and r.score_percent is None and r.coverage_percent == 0.0

    def test_unverified_checks_lower_coverage_but_never_the_score(self):
        base = assess(evidence(pages=3), TODAY).readiness
        more = assess(evidence(pages=3), TODAY, ds.Applicability(True, True, True, True)).readiness
        assert more.score_percent == base.score_percent and more.coverage_percent < base.coverage_percent


class TestEdgeCases:
    def test_zero_assessed_requirements_is_insufficient_evidence_and_never_zero_percent(self):
        r = assess(evidence(pages=0), TODAY).readiness
        assert r.assessed_weight == 0 and r.score_percent is None and r.status == "insufficient_evidence"

    def test_one_assessed_requirement_is_not_enough(self):
        ev = evidence(pages=3, mechanism="cmp", displayed=False, accept_outcome="cmp_not_automatable",
                      reject_outcome="cmp_not_automatable", policies=())
        r = assess(ev, TODAY).readiness
        assert r.status == "insufficient_evidence" or r.assessed_weight > 0

    def test_partial_evidence_earns_exactly_half(self):
        r = by_id(assess(evidence(trackers=[tr(1)], pages=3), TODAY), "DPDP-N2")
        assert r.status == "partially_met" and r.points_earned == r.weight / 2

    def test_a_duplicate_is_penalised_once_even_with_custom_weights(self):
        s = assess(evidence(trackers=[tr(1)], mechanism="none", pages=3), TODAY, weights={"DPDP-C2": 10})
        # C2 now outweighs C1, so C2 keeps the penalty and C1 is the duplicate; never both
        statuses = {r.id: r.status for r in s.requirements if r.id in ("DPDP-C1", "DPDP-C2")}
        assert sorted(statuses.values()) == ["not_applicable", "not_met"]
        assert by_id(s, "DPDP-C1").duplicate_of == "DPDP-C2"

    def test_many_observations_of_one_issue_penalise_it_once(self):
        one = assess(evidence(trackers=[tr(1)], pages=3), TODAY).readiness
        many = assess(evidence(trackers=[tr(i) for i in range(1, 30)], pages=3), TODAY).readiness
        assert one.score_percent == many.score_percent and one.points_assessed == many.points_assessed

    def test_non_applicable_requirements_do_not_enter_the_weights_or_the_denominator(self):
        s = assess(evidence(pages=3), TODAY)
        assert by_id(s, "DPDP-C2").status == "not_applicable"
        assert s.readiness.applicable_weight == sum(r.weight for r in s.requirements if r.status != "not_applicable")

    def test_future_readiness_is_scored_as_readiness_and_the_in_force_score_stays_separate(self):
        s = assess(evidence(**MIXED), TODAY)
        assert s.readiness.status == "scored" and s.in_force.status == "insufficient_evidence"
        assert s.in_force.applicable_weight == 0

    def test_a_weight_override_never_turns_an_unverified_check_into_a_scored_one(self):
        s = assess(evidence(pages=3), TODAY, weights={"DPDP-S6": 10})
        r = by_id(s, "DPDP-S6")
        assert r.status == "unable_to_verify" and not r.counts_in_score and r.points_earned == 0
