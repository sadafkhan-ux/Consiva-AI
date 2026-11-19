"""The Enterprise DPDP scoring engine (method version 2, owner's decision of 19 Nov 2026): the 100 points over 51 parameters, the three statuses, the arithmetic, the rules that stop a
score being inflated, and its independence from the 5-page agent."""
import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.enterprise.scoring import catalogue as cat
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import (
    HUMAN_REVIEW, MET, NEEDS_RECORDS, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment, ScoringError, apply_review, legal_exposure, score, status_label,
)

BEFORE = date(2026, 11, 13)       # most duties are not yet in force
AFTER = date(2027, 5, 13)         # all in force
SCORED = [p.id for p in cat.PARAMETERS if p.scored]


def ev(pid, state=MET, **kw):
    return Assessment(pid, state, evidence_refs=(f"ev:{pid}",), **kw)


def everything(state=MET, **overrides):
    """Every scored parameter at `state`, with `overrides` {id: state or Assessment or None (left out)}."""
    out = {pid: ev(pid, state) for pid in SCORED}
    for k, v in overrides.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v if isinstance(v, Assessment) else ev(k, v)
    return list(out.values())


def line(r, pid):
    return next(x for x in r.lines if x.parameter_id == pid)


# ---- the catalogue ------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_points_total_exactly_100_over_50_scored_parameters_and_51_in_all():
    assert cat.TOTAL_POINTS == 100 and len(cat.PARAMETERS) == 51 and len(SCORED) == 50 and cat.CATALOGUE_VERSION == "2.0.0"


def test_the_group_subtotals_are_the_ones_the_owner_approved():
    got = {g: sum(p.points for p in cat.PARAMETERS if p.group == g) for g in cat.GROUP_LABELS}
    assert got == {cat.NOTICE: 23, cat.CONSENT: 28, cat.CONTACT_RIGHTS: 14, cat.POLICY_DUTIES: 16, cat.CONDITIONAL: 7, cat.CROSS_CHECK: 6, cat.TECHNICAL: 6}
    assert cat.SUPPORTING_POINTS == 7


def test_the_twelve_new_parameters_carry_the_approved_marks():
    marks = {p.id: p.points for p in cat.PARAMETERS}
    assert {i: marks[i] for i in ("C10", "C11", "C12", "C13", "G9", "G10", "L1", "P1", "P2", "K2", "M2", "R1")} == {
        "C10": 2, "C11": 2, "C12": 1, "C13": 1, "G9": 1, "G10": 1, "L1": 2, "P1": 2, "P2": 1, "K2": 1, "M2": 1, "R1": 1}


def test_only_e1_is_shown_for_information_and_worth_no_points():
    assert {p.id for p in cat.PARAMETERS if not p.scored} == {"E1"}


def test_duties_a_website_cannot_show_are_judged_from_the_policy_and_scored():
    assert {p.id for p in cat.PARAMETERS if p.kind == cat.POLICY} == {"C13", "G5", "G7", "G8", "G9", "L1", "P1", "P2", "T1", "K2", "S1", "R1"}
    assert all(cat.BY_ID[i].points > 0 for i in ("G5", "G7", "G8", "S1", "T1"))


def test_a_supporting_parameter_has_no_legal_reference_and_no_penalty_ceiling():
    for p in cat.PARAMETERS:
        if p.kind == cat.SUPPORTING:
            assert p.legal_refs == () and p.penalty_ceiling_cr is None


# ---- the three statuses -------------------------------------------------------------------------------------------------------------------------------------------

def test_every_line_is_pass_partially_pass_or_fail():
    states = [MET, PARTLY, NOT_MET, NOT_ASSESSED, NOT_APPLICABLE, NEEDS_RECORDS, HUMAN_REVIEW]
    given = [ev(p.id, states[i % len(states)]) if not (states[i % len(states)] == PARTLY and not p.partial_allowed) else ev(p.id, NOT_MET) for i, p in enumerate(cat.PARAMETERS)]
    r = score(given, as_of=AFTER)
    assert {x.state for x in r.lines} <= {MET, PARTLY, NOT_MET} and {x.status for x in r.lines} <= {"Pass", "Partially Pass", "Fail"}
    assert {d["status"] for d in r.to_dict()["lines"]} <= {"Pass", "Partially Pass", "Fail"}
    assert status_label(MET) == "Pass" and status_label(PARTLY) == "Partially Pass" and status_label(NOT_MET) == "Fail"


def test_a_duty_that_does_not_apply_is_a_pass_with_the_reason():
    r = score(everything(K1=Assessment("K1", NOT_APPLICABLE, note="no page shows the site serves children")), as_of=AFTER)
    k1 = line(r, "K1")
    assert k1.state == MET and k1.earned == 2 and k1.assessed_state == NOT_APPLICABLE and k1.on_evidence
    assert "does not apply" in k1.adjustment and "children" in k1.adjustment and r.score == 100


def test_what_could_not_be_verified_is_a_fail_that_says_what_and_why():
    r = score(everything(C5=Assessment("C5", NOT_ASSESSED, note="Reject could not be given")), as_of=AFTER)
    c5 = line(r, "C5")
    assert c5.state == NOT_MET and c5.earned == 0 and not c5.on_evidence and c5.assessed_state == NOT_ASSESSED
    assert c5.adjustment.startswith("could not be verified") and "Reject could not be given" in c5.adjustment
    assert r.score == 96 and r.coverage == 96 and r.needs_attention["not_verified"] == ["C5"]


def test_needs_records_and_awaiting_a_person_are_fails_until_decided():
    r = score(everything(S1=Assessment("S1", NEEDS_RECORDS), G7=Assessment("G7", HUMAN_REVIEW)), as_of=AFTER)
    assert line(r, "S1").state == NOT_MET and line(r, "G7").state == NOT_MET and r.score == 96
    assert "published policy" in line(r, "S1").adjustment and "person" in line(r, "G7").adjustment


def test_a_parameter_nobody_assessed_is_a_fail_not_a_pass():
    r = score(everything(X1=None), as_of=AFTER)
    x1 = line(r, "X1")
    assert x1.state == NOT_MET and x1.in_play == 2 and "no assessment was supplied" in x1.adjustment and r.score == 98


def test_a_low_confidence_reading_keeps_its_result_and_is_marked_for_review():
    r = score(everything(C4=Assessment("C4", NOT_MET, ("e",), confidence="low")), as_of=AFTER)
    c4 = line(r, "C4")
    assert c4.state == NOT_MET and r.score == 96 and "reviewer" in c4.adjustment and r.needs_attention["review_recommended"] == ["C4"]


# ---- arithmetic ---------------------------------------------------------------------------------------------------------------------------------------------------

def test_everything_passed_is_100():
    r = score(everything(), as_of=AFTER)
    assert r.score == 100 and r.coverage == 100 and r.points_earned == 100 and r.points_in_play == 100


def test_a_partially_passed_parameter_earns_half_its_points_and_a_half_rounds_up():
    r = score(everything(G7=PARTLY), as_of=AFTER)            # G7 is 3 points: 100 - 1.5 = 98.5, shown as 99
    assert r.points_earned == Decimal("98.5") and r.score == 99


def test_a_half_always_rounds_up_even_where_banker_rounding_would_go_down():
    r = score(everything(G7=PARTLY, TS6=NOT_MET, TS1=NOT_MET), as_of=AFTER)         # 100 - 1.5 - 1 - 1 = 96.5: half up is 97, round-half-even would give 96
    assert r.points_earned == Decimal("96.5") and r.score == 97


def test_a_fail_earns_nothing():
    assert score(everything(C4=NOT_MET), as_of=AFTER).score == 96          # C4 is 4 points


def test_nothing_verifiable_gives_no_score_and_says_why():
    r = score([], as_of=AFTER)
    assert r.score is None and r.coverage == 0 and r.reason_no_score


def test_e1_moves_no_points_whatever_it_shows():
    assert score(everything() + [ev("E1", MET)], as_of=AFTER).score == 100
    assert line(score(everything(), as_of=AFTER), "E1").earned == 0


# ---- the rules that stop a score being inflated -------------------------------------------------------------------------------------------------------------------

def test_the_same_parameter_cannot_be_counted_twice():
    with pytest.raises(ScoringError, match="twice"):
        score([ev("N1"), ev("N1")], as_of=AFTER)


def test_an_unknown_parameter_or_state_is_refused():
    with pytest.raises(ScoringError, match="unknown parameter"):
        score([ev("Z9")], as_of=AFTER)
    with pytest.raises(ScoringError, match="unknown state"):
        score([Assessment("N1", "passed", ("e",))], as_of=AFTER)


def test_partially_pass_is_refused_where_the_parameter_has_no_partial_rule():
    assert cat.BY_ID["N1"].partial_allowed is False
    with pytest.raises(ScoringError, match="no partial rule"):
        score([ev("N1", PARTLY)], as_of=AFTER)


def test_a_result_found_by_an_assessor_needs_evidence():
    with pytest.raises(ScoringError, match="needs evidence"):
        score([Assessment("N1", MET)], as_of=AFTER)
    score([Assessment("N1", NOT_ASSESSED)], as_of=AFTER)             # no evidence is fine for what could not be verified (it becomes a Fail with the reason)


def test_a_reviewers_decision_stands_even_where_the_model_was_unsure():
    base = Assessment("C4", NOT_MET, ("e",), confidence="low")
    reviewed = apply_review(base, reviewer="A. Rao", state=MET, reason="Checked the cookie list against the policy")
    r = score(everything(C4=reviewed), as_of=AFTER)
    assert line(r, "C4").state == MET and r.score == 100 and line(r, "C4").adjustment is None
    with pytest.raises(ScoringError):
        apply_review(base, reviewer=" ", state=MET, reason="x")


def test_evidence_references_are_listed_once():
    r = score([Assessment("N1", MET, ("a", "a", "b"))], as_of=AFTER)
    assert r.lines[0].evidence_refs == ("a", "b")


def test_the_supporting_parameters_are_worth_7_of_the_100_points_and_are_never_scaled():
    assert cat.SUPPORTING_POINTS == 7 and cat.SUPPORTING_POINTS * 10 <= cat.TOTAL_POINTS
    assert score(everything(), as_of=AFTER).supporting_scale is None


# ---- date, risk label, reproducibility ------------------------------------------------------------------------------------------------------------------------------

def test_before_13_may_2027_the_score_is_a_readiness_score_and_names_what_is_not_yet_in_force():
    r = score(everything(), as_of=BEFORE)
    assert r.basis == "readiness" and "N1" in r.not_yet_in_force and "M1" not in r.not_yet_in_force and "M2" not in r.not_yet_in_force      # Consent Managers start 13 Nov 2026
    assert score(everything(), as_of=AFTER).basis == "assessment" and score(everything(), as_of=AFTER).not_yet_in_force == []


def test_the_legal_ceiling_is_the_acts_schedule_and_only_for_duties():
    r = score(everything(C4=NOT_MET, K1=PARTLY, TS4=PARTLY, G7=NOT_MET), as_of=AFTER)
    exposure = {e["parameter_id"]: e for e in legal_exposure(r)}
    assert set(exposure) == {"C4", "K1", "G7"}                              # TS4 is a supporting signal: no ceiling
    assert (exposure["C4"]["maximum_penalty_crore"], exposure["C4"]["schedule_item"]) == (50, 7)
    assert (exposure["K1"]["maximum_penalty_crore"], exposure["K1"]["schedule_item"]) == (200, 3)
    assert (exposure["G7"]["maximum_penalty_crore"], exposure["G7"]["schedule_item"]) == (250, 1)
    assert legal_exposure(r)[0]["parameter_id"] == "G7"                     # the highest ceiling first


def test_the_same_input_gives_the_same_result_in_any_order():
    a = everything(G7=PARTLY, C4=NOT_MET, TS4=PARTLY)
    r1, r2 = score(a, as_of=AFTER), score(list(reversed(a)), as_of=AFTER)
    assert r1.to_dict() == r2.to_dict() and len(r1.catalogue_fingerprint) == 16 and r1.version == "ent-dpdp-2.0.0"


def test_every_line_has_a_calculation_a_reader_can_check():
    r = score(everything(N2=PARTLY), as_of=AFTER)
    assert next(x for x in r.to_dict()["lines"] if x["parameter_id"] == "N2")["calculation"] == "N2: Partially Pass, credit 0.5 x 4 points = 2.0"
    assert r.by_group[cat.NOTICE]["points_available"] == 23 and r.by_group[cat.NOTICE]["points_earned"] == 21.0


# ---- independence -------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_enterprise_scoring_imports_nothing_from_the_5_page_agents_scoring_or_the_app_at_large():
    for f in (Path(eng.__file__), Path(cat.__file__)):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        app_imports = {m for m in names if m.startswith("app.")}
        assert app_imports <= {"app.enterprise.scoring.catalogue"}, (f.name, app_imports)
