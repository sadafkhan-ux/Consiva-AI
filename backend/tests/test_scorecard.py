"""The DPDP Website Readiness Scorecard (scoring/scorecard.py): ten rows, 100 marks, the decision table of every row, and the rules the scorecard must keep.

Built on the 24 detailed checks of the Scanner Score; the scorecard decides each row from them by a written table (docs/SCORECARD.md).
"""

import copy

import pytest

from app.api.v1.routes.consent_scans import AssessmentResponse
from app.scoring import domain_score as ds
from app.scoring import scorecard as sc
from app.services import assessment_service as svc
from tests.test_deep_parameters import SIGNALS, WITHDRAWN_OK, _without, banner_with
from tests.test_domain_score import (
    BANNER,
    FULL_POLICY,
    GA,
    HOME,
    _consent,
    _evidence,
    _pages,
    _req,
    _scan_pages,
)

WHEN = "2026-11-05T10:00:00+00:00"
FORM = {"id": "f1", "selector": "form#c", "page_url": HOME, "fields": [{"name": "email", "field_type": "email", "checked": None}, {"name": "agree", "field_type": "checkbox", "label": "I agree to the privacy policy", "checked": False}],
        "submit_url": "https://x.test/send"}
SIG_FORM = {**SIGNALS, "form_signals": [{"page_url": HOME, "selector": "form#c", "notice_link": True}]}


def go(ev=None, policy=FULL_POLICY, mode="deep", visual=None, signals=None, sp=None):
    return sc.assess_scorecard(ev or _evidence(), copy.deepcopy(policy), sp or _scan_pages(), visual or _consent(), HOME, mode=mode, scanned_at=WHEN, scan_signals=signals)


def R(r, rid):
    return next(x for x in r["rows"] if x["id"] == rid)


def banner_ev(**kw):
    return _evidence(consent_signals=BANNER, **kw)


# ── the scorecard itself ──
def test_twelve_rows_in_three_categories_weighted_to_100():
    assert [(r[0], r[3]) for r in sc.ROWS] == [("P1", 8), ("P2", 8), ("P3", 12), ("P4", 12), ("P5", 8), ("P6", 6), ("P7", 10), ("P8", 10), ("P9", 10), ("P10", 8), ("P11", 4), ("P12", 4)]
    assert sum(r[3] for r in sc.ROWS) == 100 and all(r[3] % 2 == 0 for r in sc.ROWS)
    r = go()
    assert [c["category"] for c in r["categories"]] == ["Cookie Consent", "Privacy Notice", "Forms & Security"] and [c["marks"] for c in r["categories"]] == [48, 36, 16]
    assert len(r["rows"]) == 12 and len(r["details"]) == 24                       # the 24 detailed checks stay inside, under the rows


def test_every_internal_check_belongs_to_exactly_one_row_so_no_finding_is_counted_twice():
    used = [i for r in sc.ROWS for i in r[4]]
    assert len(used) == len(set(used)) and set(used) <= {p[0] for p in ds.PARAMETERS}


def test_pass_partial_fail_and_the_two_ways_a_row_is_left_out():
    r = go(ev=banner_ev(forms=[FORM]), signals=SIG_FORM)
    assert all(x["outcome"] in ("pass", "not_assessed") for x in r["rows"]), [(x["id"], x["outcome"]) for x in r["rows"]]
    assert all(x["awarded"] == x["marks"] for x in r["rows"] if x["outcome"] == "pass")
    half = go(policy=_without(FULL_POLICY, "notice_purposes"))
    assert R(half, "P7")["outcome"] == "partial" and R(half, "P7")["awarded"] == 5 and R(half, "P7")["marks"] == 10                # partial is half, a whole number
    na = go(ev=_evidence())
    assert R(na, "P1")["outcome"] == "not_applicable" and R(na, "P1")["awarded"] is None


# ── P1 to P5 ──
def test_p1_not_applicable_when_nothing_is_tracked_and_the_rows_that_depend_on_a_banner_follow():
    r = go()
    for rid in ("P1", "P2", "P4", "P5"):
        assert R(r, rid)["outcome"] == "not_applicable", rid
    assert R(r, "P3")["outcome"] == "pass"                                       # nothing runs before a choice: still true, and still marked


def test_p1_fails_where_tracking_has_no_banner_and_the_blocked_rows_name_it():
    r = go(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    assert R(r, "P1")["outcome"] == "fail" and R(r, "P3")["outcome"] == "fail"
    for rid in ("P2", "P4", "P5"):
        assert R(r, rid)["outcome"] == "fail" and R(r, rid)["blocked_by"] == "P1", rid
    assert next(f for f in r["findings"] if f["ref"] == "P1")["consequences"] == ["P2", "P4", "P5"]        # one strong finding with its consequences (L-05)


def test_p1_partial_when_the_banner_is_missing_on_an_inner_page_or_describes_no_purposes():
    sig = {**SIGNALS, "banner_by_page": [{"url": HOME, "banner": True}, {"url": HOME + "a", "banner": False}]}
    r = go(ev=banner_ev(), signals=sig)
    assert R(r, "P1")["outcome"] == "partial" and "not on 1 inner page" in R(r, "P1")["found"]
    nogranular = [{**BANNER[0], "has_granular_choices": False}]
    assert R(go(ev=_evidence(consent_signals=nogranular)), "P1")["outcome"] == "partial"
    assert R(go(ev=banner_ev(), signals={**SIGNALS, "banner_by_page": [{"url": HOME, "banner": True}]}), "P1")["outcome"] == "pass"


def test_a_reject_control_found_but_not_clickable_is_not_assessed_never_failed():
    """Under load a click can fail on a control that exists. That is the scan's limit, not the site's failure: 0 marks for it was the cause of the same site scoring 26 and 29."""
    failed_click = [{**BANNER[0], "has_reject_all": False, "evidence": {"reject_interaction": "click_failed", "accept_interaction": "clicked"}}]
    p2 = R(go(ev=_evidence(consent_signals=failed_click)), "P2")
    assert p2["outcome"] == "not_assessed" and "could not be clicked" in p2["reason"]
    not_there = [{**BANNER[0], "has_reject_all": False, "evidence": {"reject_interaction": "not_found", "accept_interaction": "clicked"}}]
    assert R(go(ev=_evidence(consent_signals=not_there)), "P2")["outcome"] == "fail"           # a control the scan looked for and did not find still fails


def test_p2_reject_row():
    assert R(go(ev=banner_ev()), "P2")["outcome"] == "pass"
    assert R(go(ev=_evidence(consent_signals=[{**BANNER[0], "has_reject_all": False}])), "P2")["outcome"] == "fail"
    deemphasised = [{**BANNER[0], "evidence": {"button_comparison": {"comparable": False, "why": "Reject is a text link; Accept is a filled button"}}}]
    r = go(ev=_evidence(consent_signals=deemphasised))
    assert R(r, "P2")["outcome"] == "partial" and "text link" in R(r, "P2")["found"]


def test_p3_before_a_choice_pass_partial_fail_and_storage_identifiers():
    assert R(go(ev=banner_ev()), "P3")["outcome"] == "pass"
    leaking = {**GA, "consent_states": ["pre_consent", "post_accept"]}
    assert R(go(ev=banner_ev(trackers=[leaking]), visual=_consent(known=(1, 1, 0))), "P3")["outcome"] == "fail"
    stored = {**SIGNALS, "storage_signals": {"pre_consent_tracking_keys": ["_ga", "ajs_user_id"]}}
    r = go(ev=banner_ev(), signals=stored)
    assert R(r, "P3")["outcome"] == "partial" and "ajs_user_id" in R(r, "P3")["found"]


def test_p4_reject_blocks_across_pages():
    assert R(go(ev=banner_ev()), "P4")["outcome"] == "pass"
    after = {**GA, "consent_states": ["pre_consent", "post_accept", "post_reject"]}
    assert R(go(ev=banner_ev(trackers=[after]), visual=_consent(known=(1, 1, 1))), "P4")["outcome"] == "fail"
    rp = {"further_pages": [{"url": "https://x.test/a", "loaded": True, "non_essential": [{"vendor": "Google Analytics", "category": "analytics", "host": "g"}]}, {"url": "https://x.test/b", "loaded": True, "non_essential": []}]}
    leaky = [{**BANNER[0], "evidence": {"reject_persistence": rp}}]
    r = go(ev=_evidence(consent_signals=leaky))
    assert R(r, "P4")["outcome"] == "partial" and "1 of 2 further pages" in R(r, "P4")["found"]
    assert R(go(ev=banner_ev(), visual=_consent(reject=False)), "P4")["outcome"] == "not_assessed"        # Reject could not be tested: left out, never failed (L-03)


def test_p5_judges_behaviour_only_and_never_the_notice_wording():
    w_ok = go(ev=_evidence(consent_signals=banner_with(WITHDRAWN_OK)))
    assert R(w_ok, "P5")["outcome"] == "pass"
    no_notice_route = go(ev=_evidence(consent_signals=banner_with(WITHDRAWN_OK)), policy=_without(FULL_POLICY, "consent_withdrawal"))
    assert R(no_notice_route, "P5")["outcome"] == "pass" and R(no_notice_route, "P9")["outcome"] == "partial"      # the missing sentence costs P9 once, not P5 as well
    assert R(go(ev=banner_ev(), mode="quick"), "P5")["outcome"] == "not_assessed"
    none = {"tested": False, "reason": "no_reopen_control", "reopen_control": None, "withdraw_control": None, "accepted_non_essential": [], "after_withdrawal": [], "further_pages": []}
    assert R(go(ev=_evidence(consent_signals=banner_with(none))), "P5")["outcome"] == "fail"
    cont = {**WITHDRAWN_OK, "after_withdrawal": [{"vendor": "Meta Pixel", "category": "marketing", "host": "p"}]}
    assert R(go(ev=_evidence(consent_signals=banner_with(cont))), "P5")["outcome"] == "partial"


# ── P6 to P10 ──
def test_p6_notice_reachable():
    assert R(go(), "P6")["outcome"] == "pass"
    partly = {**SIGNALS, "notice_link_signals": {"pages": 5, "with_link": 3, "missing": ["https://x.test/landing"]}}
    assert R(go(signals=partly), "P6")["outcome"] == "partial"
    short = copy.deepcopy(FULL_POLICY)
    short["documents"][0]["characters"] = 900
    assert R(go(policy=short), "P6")["outcome"] == "partial" and "very little text" in R(go(policy=short), "P6")["found"]
    gone = go(ev=_evidence(policies=[], pages=_pages()[:1]), policy=None, sp=_scan_pages(1))
    assert R(gone, "P6")["outcome"] == "fail" and all(R(gone, i)["outcome"] == "fail" and R(gone, i)["blocked_by"] == "P6" for i in ("P7", "P8", "P9"))


def test_p7_data_and_purposes():
    assert R(go(), "P7")["outcome"] == "pass"
    assert R(go(policy=_without(FULL_POLICY, "notice_data_itemised")), "P7")["outcome"] == "partial"
    r = go(policy=_without(FULL_POLICY, "notice_data_itemised", "notice_purposes"))
    assert R(r, "P7")["outcome"] == "fail"
    assert R(go(policy={"status": "failed", "documents": [], "requirements": []}), "P7")["outcome"] == "not_assessed"


def test_p8_wants_business_contact_and_a_response_period_not_a_name():
    generic = copy.deepcopy(FULL_POLICY)
    generic["requirements"] = [x for x in generic["requirements"] if x["id"] != "dpo_contact"] + [_req("dpo_contact", quote="Write to us at info@x.test or call +91 98765 43210")]
    assert R(go(policy=generic), "P8")["outcome"] == "pass"                       # a business email is what s.8(9) and r.9 ask for; no name is required
    assert R(go(policy=_without(FULL_POLICY, "grievance_timeline")), "P8")["outcome"] == "partial"
    assert "a stated response period" in R(go(policy=_without(FULL_POLICY, "grievance_timeline")), "P8")["found"]
    assert R(go(policy=_without(FULL_POLICY, "dpo_contact", "grievance_contact", "grievance_timeline")), "P8")["outcome"] == "fail"


def test_p9_rights_and_the_three_routes():
    assert R(go(), "P9")["outcome"] == "pass"
    r = go(policy=_without(FULL_POLICY, "complaint_to_board"))
    assert R(r, "P9")["outcome"] == "partial" and "how to complain to the Board" in R(r, "P9")["found"]
    assert R(go(policy=_without(FULL_POLICY, "rights_means", "rights_access", "rights_correction_erasure", "rights_nominate")), "P9")["outcome"] == "fail"


def test_p10_forms_and_security():
    assert R(go(), "P10")["outcome"] == "not_applicable"                          # no form that collects personal data
    assert R(go(ev=banner_ev(forms=[FORM]), signals=SIG_FORM), "P10")["outcome"] == "pass"
    ticked = {**FORM, "fields": [FORM["fields"][0], {**FORM["fields"][1], "checked": True}]}
    r = go(ev=banner_ev(forms=[ticked]), signals=SIG_FORM)
    assert R(r, "P10")["outcome"] == "partial" and "pre-ticked" in R(r, "P10")["found"]
    insecure = {**FORM, "submit_url": "http://x.test/send"}
    assert R(go(ev=banner_ev(forms=[insecure]), signals=SIG_FORM), "P10")["outcome"] == "fail"
    mixed = {**SIG_FORM, "mixed_content": {"pages_with_insecure_content": ["https://x.test/contact"]}}
    assert R(go(ev=banner_ev(forms=[FORM]), signals=mixed), "P10")["outcome"] == "partial"


# ── score, bands, rules ──
def test_marks_not_assessed_and_not_applicable_leave_the_denominator():
    r = go(ev=banner_ev(), mode="quick")                                          # P5, P11 and P12 are not assessed (no certificate record in this evidence); P10 is not applicable
    assert r["points"]["not_assessable"] == 16 and r["points"]["not_applicable"] == 8 and r["points"]["in_play"] == 76
    assert r["coverage"] == 84


def test_the_score_is_a_whole_number_out_of_100_and_a_quick_scan_is_held_at_99():
    r = go(ev=banner_ev(), mode="quick")
    assert isinstance(r["score"], int) and r["score_text"] == f"{r['score']}/100" and r["score"] == 99 and any("held at 99" in n for n in r["band_notes"])


def test_the_workbook_thresholds_with_neutral_names_and_no_hard_fail_cap():
    assert [b[0] for b in sc.BANDS] == ["Substantially addressed", "Largely addressed", "Partially addressed", "Needs attention"] and [b[1] for b in sc.BANDS] == [90, 75, 50, 0]
    after = {**GA, "consent_states": ["pre_consent", "post_accept", "post_reject"]}
    r = go(ev=banner_ev(trackers=[after]), visual=_consent(known=(1, 1, 1)))
    assert R(r, "P3")["outcome"] == "fail" and R(r, "P4")["outcome"] == "fail"
    assert r["band"] == "Partially addressed" and r["score"] == 68                    # the number decides the band; a failed row is the first finding, not a cap on it
    assert [f["ref"] for f in r["findings"]][:2] == ["P3", "P4"]


def test_fewer_than_sixty_percent_assessable_gives_insufficient_evidence_and_no_score():
    r = go(policy={"status": "failed", "documents": [], "requirements": []}, ev=banner_ev(), visual=_consent(accept=False, reject=False), mode="quick")
    assert r["band"] == ds.INSUFFICIENT and r["score"] is None and r["indicative_score"] is not None and r["coverage"] < 60


def test_a_failed_row_is_worded_as_a_finding_with_severity_and_confidence_apart():
    r = go(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    f = next(x for x in r["findings"] if x["ref"] == "P3")
    assert f["severity"] == "High" and f["confidence"] in ("High", "Medium", "Low") and f["evidence"] and f["owner"] and f["statutory"]
    assert not ds.prohibited_wording(" ".join(x["observed"] + x["action"] for x in r["findings"]))


def test_the_gate_refuses_an_asserting_row_with_no_evidence_and_marks_on_a_row_that_asserts_nothing():
    r = go(ev=banner_ev())
    R(r, "P3")["evidence"] = []
    with pytest.raises(ds.ResultRefused):
        sc.validate_scorecard(r)
    r2 = go(ev=banner_ev(), mode="quick")
    R(r2, "P5")["awarded"] = 0
    with pytest.raises(ds.ResultRefused):
        sc.validate_scorecard(r2)


def test_same_inputs_same_result():
    import json
    assert json.dumps(go(ev=banner_ev()), sort_keys=True) == json.dumps(go(ev=banner_ev()), sort_keys=True)


# ── served through /assessment ──
def test_the_scorecard_is_served_in_the_existing_response_shape_and_is_the_default():
    from app.config import get_settings
    assert get_settings().scoring_engine == "scorecard"
    meta = {"pages_scanned": 2, "pages_discovered": 2, "accept_interaction": "clicked", "reject_interaction": "clicked"}
    r = svc.assess_parts(banner_ev(), copy.deepcopy(FULL_POLICY), meta, HOME, mode="deep", scanned_at=WHEN)
    resp = AssessmentResponse(scan_id="11111111-1111-1111-1111-111111111111", methodology_version=svc.active_version(), scan_status="completed", policy_analysis="done", final=True, **r)
    assert resp.engine == "scorecard" and len(resp.checks) == 12 and [c.ref for c in resp.checks] == [f"P{i}" for i in range(1, 13)]
    assert svc.active_version() == f"scorecard-{sc.SCORECARD_VERSION}" and resp.scanner["categories"][0]["category"] == "Cookie Consent"
    assert resp.score_text == f"{resp.score}/100" and sum(resp.counts.model_dump().values()) == 12
    assert all(rec.id == 0 or r["checks"][rec.id - 1]["ref"] for rec in resp.recommendations)


def test_the_two_earlier_engines_remain_available_for_rollback():
    meta = {"pages_scanned": 2, "pages_discovered": 2, "accept_interaction": "clicked", "reject_interaction": "clicked"}
    assert svc.assess_parts(banner_ev(), copy.deepcopy(FULL_POLICY), meta, HOME, engine="scanner")["engine"] == "scanner"
    assert svc.assess_parts(banner_ev(), copy.deepcopy(FULL_POLICY), meta, HOME, engine="ten_check")["engine"] == "ten_check"


def test_a_blocked_row_rests_on_the_evidence_of_the_row_that_blocks_it_and_withdrawal_follows_the_same_rule_in_a_quick_scan_as_in_a_deep_one():
    ev = _evidence(trackers=[GA])
    quick = go(ev=ev, visual=_consent(known=(1, 1, 0)), mode="quick")
    assert R(quick, "P5")["outcome"] == "fail" and R(quick, "P5")["blocked_by"] == "P1" and R(quick, "P1")["outcome"] == "fail"       # withdrawal is tested on every scan (3.2.0): with no banner it is blocked by P1, as in a Deep Scan
    deep = go(ev=ev, visual=_consent(known=(1, 1, 0)), mode="deep")
    assert R(deep, "P5")["outcome"] == "fail" and R(deep, "P5")["blocked_by"] == "P1" and R(deep, "P5")["evidence"] and all(e in R(deep, "P1")["evidence"] for e in R(deep, "P5")["evidence"])
