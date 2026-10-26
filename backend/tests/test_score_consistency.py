"""Identical evidence always gives the identical score; a different score always traces to different evidence.

Found while investigating a website that scored 100 in some scans and 80 in others: the scoring engine was never the variable (14 stored scans, 150 shuffles
each, no difference). The scans differed in what they RECORDED: in some, the analytics requests of the site were seen before consent; in the others they were
not. These tests pin both facts: the engine is a pure function of the evidence, and one pre-consent analytics finding costs exactly the weight of its check."""

import copy
import json
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import evidence, tr

AS_OF = date(2026, 10, 21)
BACKEND = Path(__file__).resolve().parents[1]


def cookie(i, category="analytics", states=("pre_consent",)):
    return {"id": f"c{i}", "name": f"_ck{i}", "domain": "x.test", "category": category, "vendor": None, "is_first_party": True, "source": "rule",
            "consent_states": list(states), "observations": [{"consent_state": s, "page_url": f"https://x.test/{i}"} for s in states]}


def view(score):
    return json.dumps({
        "readiness": [score.readiness.score_percent, score.readiness.status, score.readiness.points_earned, score.readiness.points_assessed,
                      score.readiness.assessed_weight, score.readiness.applicable_weight, score.readiness.coverage_percent],
        "in_force": [score.in_force.score_percent, score.in_force.points_earned],
        "requirements": [(r.id, r.status, r.points_earned, r.weight, r.counts_in_score, sorted(r.evidence_ids), list(r.observations)) for r in score.requirements],
        "deductions": [(s.requirement_id, s.points_lost, s.percent_lost, s.score_before, s.score_after) for s in score.deductions.steps],
    }, sort_keys=True, default=str)


SCENARIOS = {
    "clean": {},
    "pre_consent_tracker": {"trackers": [tr(1)]},
    "tracker_and_cookie": {"trackers": [tr(1), tr(2, "marketing")], "cookies": [cookie(1), cookie(2, "advertising")]},
    "no_reject_no_granular": {"trackers": [tr(1)], "reject": False, "granular": False},
    "no_policy": {"policies": ()},
    "many_pages_many_trackers": {"pages": 5, "trackers": [tr(i, "analytics" if i % 2 else "marketing", ("pre_consent", "post_accept")) for i in range(1, 12)]},
    "unconfirmed_reject": {"confirmed": False, "trackers": [tr(1, "analytics", ("post_reject",))]},
    "framed": {"framed": ("corp.example",), "mechanism": "none"},
}


class TestSameEvidenceSameScore:
    @pytest.mark.parametrize("name", SCENARIOS)
    def test_the_row_order_never_changes_the_score(self, name):
        ev = evidence(**SCENARIOS[name])
        base = view(assess(ev, AS_OF))
        for seed in range(60):
            shuffled = copy.deepcopy(ev)
            rnd = random.Random(seed)
            for value in shuffled.values():
                if isinstance(value, list):
                    rnd.shuffle(value)
            assert view(assess(shuffled, AS_OF)) == base, f"{name}: the score depends on the order of the evidence rows (seed {seed})"

    @pytest.mark.parametrize("name", SCENARIOS)
    def test_assessing_twice_gives_the_same_result(self, name):
        ev = evidence(**SCENARIOS[name])
        assert view(assess(ev, AS_OF)) == view(assess(copy.deepcopy(ev), AS_OF))

    def test_the_input_is_never_changed_by_scoring(self):
        ev = evidence(**SCENARIOS["tracker_and_cookie"])
        before = json.dumps(ev, sort_keys=True)
        assess(ev, AS_OF)
        assert json.dumps(ev, sort_keys=True) == before

    def test_canonical_order_is_by_content_not_position(self):
        a = {"trackers": [{"id": "b", "x": 1}, {"id": "a", "x": 2}], "n": 3}
        b = {"trackers": [{"id": "a", "x": 2}, {"id": "b", "x": 1}], "n": 3}
        assert ds.canonical_evidence(a) == ds.canonical_evidence(b)
        assert ds.canonical_evidence(a)["n"] == 3

    @pytest.mark.parametrize("seed", ["0", "1", "42"])
    def test_separate_processes_with_different_hash_seeds_agree(self, seed):
        """Python orders sets of strings differently in every process unless pinned; the score must not depend on it."""
        code = (
            "import json\n"
            "from tests.test_score_consistency import SCENARIOS, view, AS_OF\n"
            "from tests.test_dpdp_score import evidence\n"
            "from app.scoring.dpdp_score import assess\n"
            "print(json.dumps({k: view(assess(evidence(**v), AS_OF)) for k, v in SCENARIOS.items()}, sort_keys=True))"
        )
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(BACKEND), "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False)
        assert out.returncode == 0, out.stderr[-500:]
        expected = json.dumps({k: view(assess(evidence(**v), AS_OF)) for k, v in SCENARIOS.items()}, sort_keys=True)
        assert out.stdout.strip() == expected


class TestADifferentScoreAlwaysTracesToDifferentEvidence:
    def test_one_pre_consent_analytics_request_is_the_difference_between_100_and_a_lower_score(self):
        """With nothing analytics-like before consent every assessed check is met (100); with one request seen before consent, DPDP-C1 (weight 5) is Not Met and
        the score falls by that check's whole share. Nothing is hidden and nothing is forced."""
        clean = assess(evidence(policies=("privacy_policy", "cookie_policy")), AS_OF)
        dirty = assess(evidence(policies=("privacy_policy", "cookie_policy"), trackers=[tr(1)]), AS_OF)
        assert clean.readiness.score_percent == 100.0
        assert dirty.readiness.score_percent < 100.0                       # a genuine failure is never forced to 100
        assert next(r for r in dirty.requirements if r.id == "DPDP-C1").status == "not_met"
        assert next(r for r in clean.requirements if r.id == "DPDP-C1").status == "met"
        steps = {s.requirement_id: s for s in dirty.deductions.steps}
        assert steps["DPDP-C1"].points_lost == 5.0                         # and the deduction record names it

    def test_the_gap_between_two_scans_equals_the_deductions_of_the_checks_that_changed(self):
        a = assess(evidence(trackers=[tr(1)]), AS_OF)
        b = assess(evidence(), AS_OF)
        drop = round(b.readiness.score_percent - a.readiness.score_percent, 1)
        assert drop == pytest.approx(sum(s.percent_lost for s in a.deductions.steps), abs=0.11)

    def test_a_score_is_never_produced_from_too_little_evidence(self):
        assert assess(evidence(pages=0), AS_OF).readiness.score_percent is None

    def test_a_failure_is_never_hidden_by_other_passes(self):
        s = assess(evidence(trackers=[tr(1)], policies=("privacy_policy", "cookie_policy")), AS_OF)
        assert next(r for r in s.requirements if r.id == "DPDP-C1").status == "not_met"
