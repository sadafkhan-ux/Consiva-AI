"""The score ledger: every number on the score card, defined once and consistent with the score block it is read from.

These tests pin the property the card exists for: a reviewer can trace every aggregate back to the individual checks. Counts and
weights must add up, every percentage must be exactly numerator over denominator, the per-check contributions must add up to the
score, and the ledger must never be a second formula."""

import itertools
import random
from datetime import date

import pytest

from app.scoring import dpdp_score as ds
from tests.test_dpdp_score import evidence, tr

TODAY, AFTER = date(2026, 10, 22), date(2027, 5, 13)


def combos():
    rows = []
    for pages, mech, reject, gran, pols, trk, disp, rej_out in itertools.product(
        [0, 3], ["banner", "cmp", "none"], [True, False, None], [True, False], [(), ("privacy_policy",), ("privacy_policy", "cookie_policy")],
        [(), (tr(1),), (tr(1, states=("post_accept",)),), (tr(1, category=None),)], [True, False], ["clicked", "click_failed"],
    ):
        rows.append(dict(pages=pages, mechanism=mech, reject=reject, granular=gran, policies=pols, trackers=list(trk), displayed=disp,
                         reject_outcome=rej_out))
    random.Random(11).shuffle(rows)
    return rows[:300]


COMBOS = combos()


@pytest.mark.parametrize("as_of", [TODAY, AFTER])
def test_counts_and_weights_add_up_and_percentages_are_exactly_numerator_over_denominator(as_of):
    for kw in COMBOS:
        s = ds.assess(evidence(**kw), as_of)
        L, rb = s.ledger, s.readiness

        assert L.total_checks == len(s.requirements) == 16
        assert L.assessed_checks + L.unable_checks + L.not_applicable_checks == L.total_checks
        assert L.assessed_checks == L.passed_checks + L.partial_checks + L.failed_checks
        assert L.applicable_checks == L.total_checks - L.not_applicable_checks
        assert L.assessed_weight + L.unable_weight == L.applicable_weight
        assert L.total_weight - L.not_applicable_weight == L.applicable_weight
        assert L.assessed_weight == sum(r.weight for r in s.requirements if r.counts_in_score)
        assert L.earned_weight == pytest.approx(sum(r.points_earned for r in s.requirements))
        assert L.penalty_weight == pytest.approx(L.assessed_weight - L.earned_weight)

        by = {m.key: m for m in L.metrics}
        assert (by["score"].numerator, by["score"].denominator, by["score"].percent) == (rb.points_earned, rb.points_assessed, rb.score_percent)
        assert (by["coverage"].numerator, by["coverage"].denominator, by["coverage"].percent) == (rb.assessed_weight, rb.applicable_weight, rb.coverage_percent)
        for m in L.metrics + L.category_metrics:
            if m.percent is not None:
                assert m.percent == ds._pct(m.numerator, m.denominator), m.key


def test_every_status_rule_lists_exactly_the_checks_of_that_status_and_the_rules_partition_the_checks():
    s = ds.assess(evidence(trackers=[tr(1)], pages=3), TODAY)
    ids = []
    for rule in s.ledger.status_rules:
        in_status = [r.id for r in s.requirements if r.status == rule.status]
        assert rule.check_ids == in_status
        assert rule.count == len(in_status)
        assert rule.weight == sum(r.weight for r in s.requirements if r.status == rule.status)
        ids += rule.check_ids
    assert sorted(ids) == sorted(r.id for r in s.requirements)


def test_penalty_is_the_weight_not_earned_and_only_scored_checks_have_one():
    for kw in COMBOS[:80]:
        s = ds.assess(evidence(**kw), AFTER)
        for r in s.requirements:
            if r.counts_in_score:
                assert r.penalty_points == pytest.approx(r.weight - r.points_earned)
                assert {"met": 0, "partially_met": r.weight / 2, "not_met": r.weight}[r.status] == pytest.approx(r.penalty_points)
            else:
                assert r.penalty_points is None and r.score_contribution_percent is None


def test_the_per_check_contributions_add_up_to_the_score_within_rounding():
    for kw in COMBOS:
        s = ds.assess(evidence(**kw), AFTER)
        if s.readiness.status != "scored":
            continue
        total = sum(r.score_contribution_percent or 0 for r in s.requirements)
        assert abs(total - s.readiness.score_percent) <= 0.05 * len([r for r in s.requirements if r.counts_in_score]) + 1e-9


def test_a_check_that_could_not_be_verified_is_never_a_failure_and_a_not_applicable_check_is_never_a_pass():
    s = ds.assess(evidence(pages=3), TODAY)
    for r in s.requirements:
        if r.status == "unable_to_verify":
            assert not r.counts_in_score and r.penalty_points is None and r.points_earned == 0
            assert r.coverage_share_percent is not None          # it stays in the coverage denominator
        if r.status == "not_applicable":
            assert not r.counts_in_score and r.coverage_share_percent is None and r.points_earned == 0


def test_every_check_has_a_legal_note_and_the_note_does_not_overclaim():
    s = ds.assess(evidence(pages=3), TODAY)
    assert set(ds.LEGAL_BASIS_NOTES) == {r.id for r in ds.REQUIREMENTS}
    for r in s.requirements:
        assert r.legal_note and r.provisions
    n2 = next(r for r in s.requirements if r.id == "DPDP-N2")
    assert "do not name a cookie policy" in n2.legal_note          # a reading of the duty is labelled as one
    business = [r for r in s.requirements if r.check_type == "business"]
    assert business and all("organisational evidence" in r.legal_note or "declares" in r.legal_note or "declared" in r.legal_note
                            for r in business)


def test_the_penalty_and_security_notes_say_what_the_numbers_mean():
    L = ds.assess(evidence(pages=3), TODAY).ledger
    assert "no separate penalty list" in L.penalty_note and "1 means met" in L.penalty_note and "0 means not met" in L.penalty_note
    assert "no separate security score" in L.security_score_note
    labels = {r.status: r for r in L.status_rules}
    assert "left out of the score" in labels["unable_to_verify"].rule.lower() or "not scored" in labels["unable_to_verify"].rule.lower()
    assert labels["unable_to_verify"].on_coverage.startswith("Lowers coverage")
    assert labels["not_applicable"].on_coverage.startswith("Does not lower coverage")


def test_a_withheld_score_shows_no_percentage_but_still_explains_itself():
    s = ds.assess(evidence(pages=0), TODAY)
    score = next(m for m in s.ledger.metrics if m.key == "score")
    assert s.readiness.status == "insufficient_evidence"
    assert score.percent is None and "No score is shown" in score.meaning


def test_the_in_force_score_is_not_shown_before_any_duty_starts():
    s = ds.assess(evidence(pages=3), TODAY)
    m = next(m for m in s.ledger.metrics if m.key == "in_force")
    assert m.percent is None and m.denominator == 0


# ---- pages that did not load are not pages scanned ----------------------------------------------------------------------------

def _with_page_records(records):
    ev = evidence(pages=0)
    ev["pages"] = [{"id": f"p{i}", "url": f"https://x.test/{i}", "title": None, "http_status": 200 if via in ("seed", "link", "sitemap") else None,
                    "discovered_via": via} for i, via in enumerate(records)]
    return ev


def test_failed_and_timed_out_page_records_are_not_counted_as_pages_scanned():
    s = ds.assess(_with_page_records(["seed", "link", "sitemap", "timeout", "failed"]), TODAY)
    assert s.evidence_summary.pages_scanned == 3
    assert len(s.assessment.pages_scanned) == 3
    assert all(not u.endswith(("/3", "/4")) for u in s.assessment.pages_scanned)


def test_a_scan_where_no_page_loaded_is_unable_to_verify_not_assessed_on_nothing():
    s = ds.assess(_with_page_records(["failed", "timeout"]), TODAY)
    assert s.evidence_summary.pages_scanned == 0
    website = [r for r in s.requirements if r.check_type == "website" and r.status != "not_applicable"]
    assert website and all(r.status in ("unable_to_verify", "not_applicable") for r in website)


def test_a_page_record_with_no_stored_outcome_still_counts_as_before():
    old = evidence(pages=3)                      # the fixtures carry no discovered_via, like a scan stored before it was kept
    assert ds.assess(old, TODAY).evidence_summary.pages_scanned == 3


# ---- a page named once, however many consent states saw it ---------------------------------------------------------------------

def test_a_page_seen_in_three_consent_states_is_listed_once_in_a_finding():
    cookie = {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": "Google Analytics", "is_first_party": True,
              "source": "rule", "consent_states": ["pre_consent", "post_accept", "post_reject"],
              "observations": [{"consent_state": s, "page_url": "https://x.test/", "observed_at": None, "method": "x", "source_request_url": None,
                                "candidate_page_urls": []} for s in ("pre_consent", "post_accept", "post_reject")]}
    ev = evidence(pages=3, mechanism="cmp", displayed=True)
    ev["cookies"] = [cookie]
    s = ds.assess(ev, TODAY)
    for group in (s.finding_groups.confirmed, s.finding_groups.needs_review, s.finding_groups.future_readiness):
        for e in group:
            assert len(e.pages) == len(set(e.pages)), (e.id, e.pages)


def test_the_pre_consent_rule_finding_states_what_was_observed_and_makes_no_claim_about_how_common_it_is():
    from app.rules.consent_rules import evaluate_consent_rules
    ev = evidence(trackers=[tr(1)], pages=3)
    summary = next(f for f in evaluate_consent_rules(ev) if f.rule_id == "R-001").summary
    assert "fired BEFORE any consent interaction" in summary
    assert "most common" not in summary
