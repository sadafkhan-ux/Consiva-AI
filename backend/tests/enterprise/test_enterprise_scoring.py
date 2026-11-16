"""The Enterprise DPDP scoring engine: the 100 points, the arithmetic, the rules that stop a score being inflated, and its independence from the 5-page agent."""
import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.enterprise.scoring import catalogue as cat
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import (
    MET, NEEDS_RECORDS, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment, ScoringError, apply_review, legal_exposure, score,
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


# ---- the catalogue ------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_points_total_exactly_100_over_33_scored_parameters_and_39_in_all():
    assert cat.TOTAL_POINTS == 100 and len(cat.PARAMETERS) == 39 and len(SCORED) == 33


def test_the_group_subtotals_are_the_ones_the_owner_approved():
    got = {g: sum(p.points for p in cat.PARAMETERS if p.group == g) for g in cat.GROUP_LABELS}
    assert got == {cat.NOTICE: 30, cat.CONSENT: 33, cat.CONTACT_RIGHTS: 15, cat.CONDITIONAL: 6, cat.CROSS_CHECK: 9, cat.TECHNICAL: 7}
    assert cat.SUPPORTING_POINTS == 8


def test_what_a_website_cannot_show_earns_and_loses_nothing():
    assert {p.id for p in cat.PARAMETERS if not p.scored} == {"G5", "G7", "G8", "S1", "T1", "E1"}


def test_a_supporting_parameter_has_no_legal_reference_and_no_penalty_ceiling():
    for p in cat.PARAMETERS:
        if p.kind == cat.SUPPORTING:
            assert p.legal_refs == () and p.penalty_ceiling_cr is None


# ---- arithmetic ---------------------------------------------------------------------------------------------------------------------------------------------------

def test_everything_met_is_100():
    r = score(everything(), as_of=AFTER)
    assert r.score == 100 and r.coverage == 100 and r.points_earned == 100 and r.points_in_play == 100


def test_a_partly_met_parameter_earns_half_its_points_and_a_half_rounds_up():
    r = score(everything(N2=PARTLY), as_of=AFTER)            # N2 is 5 points: 100 - 2.5 = 97.5, shown as 98
    assert r.points_earned == Decimal("97.5") and r.score == 98


def test_a_half_always_rounds_up_even_where_banker_rounding_would_go_down():
    r = score(everything(N2=PARTLY, TS6=NOT_MET), as_of=AFTER)         # 100 - 2.5 - 1 = 96.5: half up is 97, round-half-even would give 96
    assert r.points_earned == Decimal("96.5") and r.score == 97


def test_not_met_earns_nothing():
    assert score(everything(C4=NOT_MET), as_of=AFTER).score == 95          # C4 is 5 points


def test_a_parameter_that_does_not_apply_earns_nothing_so_its_points_are_lost():
    """Owner's decision, 16 Nov 2026: a parameter that does not apply is not left out and not given in full: it earns no points."""
    r = score(everything(K1=NOT_APPLICABLE, M1=NOT_APPLICABLE), as_of=AFTER)          # 6 points not earned, the rest all met
    assert r.points_earned == 94 and r.score == 94 and r.coverage == 94
    r2 = score(everything(K1=NOT_APPLICABLE, M1=NOT_APPLICABLE, N2=NOT_MET), as_of=AFTER)          # N2 is 5 more points
    assert r2.score == 89


def test_a_parameter_nobody_assessed_is_not_assessed_not_passed():
    r = score(everything(X1=None), as_of=AFTER)
    line = next(x for x in r.lines if x.parameter_id == "X1")
    assert line.state == NOT_ASSESSED and line.in_play == 0 and r.points_in_play == 97


def test_the_owners_example_of_six_parameters_is_the_points_it_earned_out_of_100():
    """Six parameters earned 16.5 points; everything else earned nothing; the score is 16.5 out of 100, shown as 17 (half up)."""
    six = [ev("N2"), ev("N3", PARTLY), ev("C4", NOT_MET), ev("G1"), ev("G2"), ev("N7")]
    r = score(six, as_of=AFTER)
    assert (r.points_earned, r.points_in_play, r.coverage) == (Decimal("16.5"), 24, 24)
    assert r.score == 17 and r.reason_no_score is None


def test_there_is_no_minimum_coverage_and_a_parameter_left_unassessed_costs_its_points():
    notice_consent = [p.id for p in cat.PARAMETERS if p.group in (cat.NOTICE, cat.CONSENT) and p.scored and p.kind != cat.SUPPORTING]     # 62 points
    assert score([ev(i) for i in notice_consent], as_of=AFTER).score == 62
    fewer = [i for i in notice_consent if i not in ("C1", "C2", "C9")]       # 53 points assessed and earned: the other 9 earn nothing
    r = score([ev(i) for i in fewer], as_of=AFTER)
    assert r.score == 53 and r.coverage == 53
    assert "minimum" not in str(r.to_dict()).lower() and not hasattr(r, "minimum_coverage")


def test_nothing_assessed_gives_no_score_and_says_why():
    r = score([], as_of=AFTER)
    assert r.score is None and r.points_in_play == 0 and r.reason_no_score


# ---- the rules that stop a score being inflated -------------------------------------------------------------------------------------------------------------------

def test_the_same_parameter_cannot_be_counted_twice():
    with pytest.raises(ScoringError, match="twice"):
        score([ev("N1"), ev("N1")], as_of=AFTER)


def test_an_unknown_parameter_or_state_is_refused():
    with pytest.raises(ScoringError, match="unknown parameter"):
        score([ev("Z9")], as_of=AFTER)
    with pytest.raises(ScoringError, match="unknown state"):
        score([Assessment("N1", "passed", ("e",))], as_of=AFTER)


def test_partly_met_is_refused_where_the_parameter_has_no_partial_rule():
    assert cat.BY_ID["N1"].partial_allowed is False
    with pytest.raises(ScoringError, match="no partial rule"):
        score([ev("N1", PARTLY)], as_of=AFTER)


def test_a_result_that_earns_or_loses_points_needs_evidence():
    with pytest.raises(ScoringError, match="needs evidence"):
        score([Assessment("N1", MET)], as_of=AFTER)
    score([Assessment("N1", NOT_ASSESSED)], as_of=AFTER)             # no evidence is fine for what was not assessed


def test_a_low_confidence_model_reading_goes_to_a_person_and_moves_no_points():
    r = score(everything(C4=Assessment("C4", NOT_MET, ("e",), confidence="low")), as_of=AFTER)
    line = next(x for x in r.lines if x.parameter_id == "C4")
    assert line.state == "human_review" and line.in_play == 0 and r.points_in_play == 95 and r.score == 95          # awaiting a person: the 5 points are not earned yet
    assert r.needs_attention["human_review"] == ["C4"] and "person" in line.adjustment


def test_a_reviewers_decision_stands_even_where_the_model_was_unsure():
    base = Assessment("C4", NOT_MET, ("e",), confidence="low")
    reviewed = apply_review(base, reviewer="A. Rao", state=MET, reason="Checked the cookie list against the policy")
    r = score(everything(C4=reviewed), as_of=AFTER)
    line = next(x for x in r.lines if x.parameter_id == "C4")
    assert line.state == MET and r.score == 100
    with pytest.raises(ScoringError):
        apply_review(base, reviewer=" ", state=MET, reason="x")


def test_evidence_references_are_listed_once():
    r = score([Assessment("N1", MET, ("a", "a", "b"))], as_of=AFTER)
    assert r.lines[0].evidence_refs == ("a", "b")


def test_the_supporting_parameters_are_worth_8_of_the_100_points_and_are_never_scaled():
    assert cat.SUPPORTING_POINTS == 8 and cat.SUPPORTING_POINTS * 10 <= cat.TOTAL_POINTS
    r = score([ev("N1")] + [ev(p.id) for p in cat.PARAMETERS if p.kind == cat.SUPPORTING and not p.partial_allowed], as_of=AFTER)
    assert r.supporting_scale is None and not any(x.adjustment and "capped" in x.adjustment for x in r.lines)
    no_partial = sum(p.points for p in cat.PARAMETERS if p.kind == cat.SUPPORTING and not p.partial_allowed)       # C6, TS1, TS2, TS3, TS6: 1 point each
    assert no_partial == 5 and r.points_earned == 3 + no_partial          # N1 (3) and those five


def test_with_everything_assessed_the_supporting_parameters_are_not_scaled():
    assert score(everything(), as_of=AFTER).supporting_scale is None


def test_a_statement_only_parameter_is_shown_as_found_and_moves_no_points():
    r = score(everything() + [ev("G7", NOT_MET)], as_of=AFTER)
    line = next(x for x in r.lines if x.parameter_id == "G7")
    assert line.state == NOT_MET and line.points == 0 and line.earned == 0 and "no points" in line.adjustment
    assert r.score == 100 and legal_exposure(r) == []


# ---- date, risk label, reproducibility ------------------------------------------------------------------------------------------------------------------------------

def test_before_13_may_2027_the_score_is_a_readiness_score_and_names_what_is_not_yet_in_force():
    r = score(everything(), as_of=BEFORE)
    assert r.basis == "readiness" and "N1" in r.not_yet_in_force and "M1" not in r.not_yet_in_force      # Consent Managers start 13 Nov 2026
    assert score(everything(), as_of=AFTER).basis == "assessment" and score(everything(), as_of=AFTER).not_yet_in_force == []


def test_the_legal_ceiling_is_the_acts_schedule_and_only_for_statutory_parameters():
    r = score(everything(C4=NOT_MET, K1=PARTLY, TS4=PARTLY, N2=MET), as_of=AFTER)
    exposure = {e["parameter_id"]: e for e in legal_exposure(r)}
    assert set(exposure) == {"C4", "K1"}                                    # TS4 is a supporting signal: no ceiling
    assert (exposure["C4"]["maximum_penalty_crore"], exposure["C4"]["schedule_item"]) == (50, 7)
    assert (exposure["K1"]["maximum_penalty_crore"], exposure["K1"]["schedule_item"]) == (200, 3)
    assert legal_exposure(r)[0]["parameter_id"] == "K1"                     # the highest ceiling first


def test_the_same_input_gives_the_same_result_in_any_order():
    a = everything(N2=PARTLY, C4=NOT_MET, TS4=PARTLY)
    r1, r2 = score(a, as_of=AFTER), score(list(reversed(a)), as_of=AFTER)
    assert r1.to_dict() == r2.to_dict() and len(r1.catalogue_fingerprint) == 16 and r1.version == "ent-dpdp-1.0.0"


def test_every_line_has_a_calculation_a_reader_can_check():
    r = score(everything(N2=PARTLY), as_of=AFTER)
    assert next(x for x in r.to_dict()["lines"] if x["parameter_id"] == "N2")["calculation"] == "N2: partly met, credit 0.5 x 5 points = 2.5"
    assert r.by_group[cat.NOTICE]["points_available"] == 30 and r.by_group[cat.NOTICE]["points_earned"] == 27.5


def test_a_parameter_that_needs_records_stays_out_of_the_score():
    r = score(everything() + [Assessment("S1", NEEDS_RECORDS)], as_of=AFTER)
    assert r.score == 100 and r.needs_attention["needs_records"] == ["S1"]


# ---- independence -------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_enterprise_scoring_imports_nothing_from_the_5_page_agents_scoring_or_the_app_at_large():
    for f in (Path(eng.__file__), Path(cat.__file__)):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        app_imports = {m for m in names if m.startswith("app.")}
        assert app_imports <= {"app.enterprise.scoring.catalogue"}, (f.name, app_imports)
