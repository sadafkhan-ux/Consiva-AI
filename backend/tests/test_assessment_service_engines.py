"""/assessment serves the Scanner Score by default and the ten-check engine on rollback, in the same fields, through the same response model."""

import copy

import pytest

from app.api.v1.routes.consent_scans import AssessmentResponse
from app.services import assessment_service as svc
from tests.test_domain_score import FULL_POLICY, HOME, _consent, _evidence, _scan_pages


def _parts():
    return _evidence(), copy.deepcopy(FULL_POLICY)


def _meta():
    return {"pages_scanned": 2, "pages_discovered": 2, "accept_interaction": "clicked", "reject_interaction": "clicked"}


def _full(result, **kw):
    return AssessmentResponse(scan_id="11111111-1111-1111-1111-111111111111", methodology_version=svc.active_version(), scan_status="completed", policy_analysis="done", final=True, **result, **kw)


def test_scanner_result_fits_the_existing_response_model_and_carries_the_whole_result():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, mode="quick", scanned_at="2026-11-05T10:00:00+00:00", engine="scanner")
    resp = _full(r)
    assert resp.engine == "scanner" and resp.scanner["band"] and resp.scanner["scale"] == 100
    assert resp.score is not None and resp.score_text == f"{resp.score}/100" and "." not in resp.score_text
    assert resp.checks and all(c.ref for c in resp.checks)


def test_quick_scan_lists_every_parameter_and_marks_the_ones_it_cannot_reach_as_deep_scan_only():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, mode="quick", engine="scanner")
    assert len(r["checks"]) == 24
    beyond = [c for c in r["checks"] if c["marks_text"] == "Deep Scan only"]
    assert {c["ref"] for c in beyond} == {"A5", "B5", "B6", "C4", "D1", "D2", "E1", "E2", "E3", "F2", "G2"}
    assert all(c["result"] == "Not Assessed" and c["awarded"] is None and c["max"] is None and "Deep Scan" in c["explanation"] for c in beyond)
    assert sum(x["points"] for x in r["scanner"]["beyond_quick_scan"]) == 30
    assert any("Run a Deep Scan" in x["text"] for x in r["recommendations"]) and not any("A Quick Scan assesses" in x for x in r["limitations"])
    assert r["score"] == svc.assess_parts(ev, pol, _meta(), HOME, mode="quick", engine="scanner")["score"]      # listing them changes no mark


def test_deep_scan_lists_every_parameter():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, mode="deep", engine="scanner")
    assert len(r["checks"]) == 24 and r["scanner"]["scale"] == 100


def test_counts_and_marks_are_consistent_with_the_checks():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, engine="scanner")
    c = r["counts"]
    assert sum(c.values()) == len(r["checks"])
    assert r["awarded"] == sum(x["awarded"] for x in r["checks"] if x["awarded"] is not None)
    assert r["applicable_max"] == sum(x["max"] for x in r["checks"] if x["max"] is not None)


def test_rollback_serves_the_ten_check_engine_with_no_scanner_object():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, engine="ten_check")
    resp = _full(r)
    assert resp.engine == "ten_check" and resp.scanner is None and len(resp.checks) == 10


def test_the_setting_chooses_the_engine_and_its_version_keys_the_cache(monkeypatch):
    from app.config import get_settings
    ev, pol = _parts()
    s = get_settings()
    monkeypatch.setattr(s, "scoring_engine", "ten_check")
    assert svc.active_engine() == "ten_check" and svc.active_version().startswith("ten-check-")
    assert svc.assess_parts(ev, pol, _meta(), HOME)["engine"] == "ten_check"
    monkeypatch.setattr(s, "scoring_engine", "scanner")
    assert svc.active_version().startswith("scanner-") and svc.assess_parts(ev, pol, _meta(), HOME)["engine"] == "scanner"


def test_the_setting_refuses_an_unknown_engine():
    from pydantic import ValidationError

    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(scoring_engine="weighted")


def test_application_profile_is_a_deep_scan_and_everything_else_quick():
    assert svc.scan_mode({"profile": "application"}) == "deep"
    assert svc.scan_mode({}) == "quick" and svc.scan_mode(None) == "quick" and svc.scan_mode({"profile": "nonsense"}) == "quick"


def test_a_scan_with_no_evidence_is_served_without_a_score_and_without_error():
    r = svc.assess_parts(None, None, None, None, engine="scanner")
    assert r["score"] is None and r["score_text"] == "No meaningful score"
    _full(r)


def test_nothing_the_dashboard_prints_shows_a_score_out_of_anything_but_100():
    import re
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, mode="quick", engine="scanner")
    printed = " ".join([r["score_text"], r["total_text"], r["summary"], r["formula"], *r["limitations"]])
    assert not re.search(r"\d+\s*/\s*(?!100(?!\d))\d+", printed) and "ceiling" not in printed.lower()
    assert r["total_text"] == r["score_text"]


def test_a_recommendation_names_the_check_number_in_the_table_never_its_place_in_the_list():
    ev, pol = _parts()
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] not in ("dpo_contact", "grievance_contact", "complaint_to_board", "consent_withdrawal")]
    r = svc.assess_parts(ev, pol, _meta(), HOME, engine="scanner")
    by_id = {c["id"]: c["ref"] for c in r["checks"]}
    assert r["recommendations"], r
    for rec in r["recommendations"]:
        if rec["id"]:
            assert by_id[rec["id"]] in {f["ref"] for f in r["scanner"]["priority_actions"]}       # "Check 9" is the check numbered 9 in the table
    first = r["scanner"]["priority_actions"][0]["ref"]
    assert by_id[r["recommendations"][0]["id"]] == first


def test_the_summary_does_not_repeat_the_score_and_each_check_cites_its_own_provisions():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, engine="scanner")
    assert not r["summary"].startswith(r["score_text"]) and r["summary"].count(r["score_text"]) == 0
    a4 = next(c for c in r["checks"] if c["ref"] == "A4")
    assert a4["legal"].startswith("DPDP Act s.5(1)(iii), s.6(4); DPDP Rules r.3(c)(i)") and "r.3(c)(iii)" in a4["legal"]


def test_a_partly_given_notice_reads_as_a_sentence():
    ev, pol = _parts()
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] != "consent_withdrawal"]
    r = svc.assess_parts(ev, pol, _meta(), HOME, engine="scanner")
    a4 = next(c for c in r["checks"] if c["ref"] == "A4")
    assert a4["explanation"] == "The notice gives a route to exercise rights and a route to complain to the Board, but no route to withdraw consent."


def test_check_numbers_run_without_gaps_with_the_reachable_parameters_first_and_counts_do_not_blame_the_scan():
    ev, pol = _parts()
    r = svc.assess_parts(ev, pol, _meta(), HOME, mode="quick", engine="scanner")
    assert [c["id"] for c in r["checks"]] == list(range(1, 25))
    assert [c["ref"] for c in r["checks"][:13]] == ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "F1", "G1"]
    assert all(c["marks_text"] == "Deep Scan only" for c in r["checks"][13:])
    assert r["counts"]["beyond_scan_type"] == 11 and r["counts"]["not_assessed"] == 0              # "could not be assessed" is for what the scan tried and could not do
    assert r["incomplete"] == 0 and sum(r["counts"].values()) == 24
