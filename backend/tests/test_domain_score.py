"""The Scanner Score (scoring/domain_score.py): the seven-domain, 100-point model of the Scoring Mechanism v1.0.

Each test builds the small scan it needs and checks the parameter state, the arithmetic and the guardrails the two documents require.
"""

import copy

import pytest

from app.scoring import domain_score as ds

HOME = "https://x.test/"
NOTICE = "https://x.test/privacy-policy"


def _req(i, status="verified", quote="We state this clearly in our notice."):
    return {"id": i, "status": status, "quote": quote, "source_url": NOTICE}


FULL_POLICY = {
    "status": "analysed",
    "documents": [{"url": NOTICE, "policy_type": "privacy_policy", "characters": 9000, "truncated": False}],
    "requirements": [
        _req("notice_data_itemised"), _req("notice_purposes"), _req("consent_withdrawal"), _req("rights_means"), _req("rights_access"), _req("rights_correction_erasure"),
        _req("rights_nominate"), _req("grievance_contact", quote="Grievance Officer: Mr Rahul Sharma, grievance@x.test, +91 98765 43210"), _req("grievance_timeline"),
        _req("dpo_contact", quote="Data Protection Officer: Mr Rahul Sharma, dpo@x.test"),
        _req("data_sharing", quote="We share data with analytics providers, advertising providers and hosting providers."), _req("retention"),
        _req("complaint_to_board", quote="You may complain to the Data Protection Board of India if we do not resolve your grievance."), _req("security", quote="We encrypt personal data in transit and at rest."),
        _req("breach_response", quote="We will inform you and the Board of a personal data breach without delay."), _req("cookies_tracking", quote="We use cookies for analytics and advertising with your consent."),
        _req("lawful_basis", quote="We process data on your consent and for the legitimate uses the Act allows."),
    ],
}


def _pages(*extra):
    base = [{"url": HOME, "discovered_via": "seed", "http_status": 200}, {"url": NOTICE, "discovered_via": "link", "http_status": 200}]
    return base + list(extra)


def _evidence(**kw):
    ev = {"pages": _pages(), "policies": [{"url": NOTICE, "policy_type": "privacy_policy", "text_state": "complete"}], "cookies": [], "trackers": [], "forms": [],
          "consent_signals": [{"mechanism_type": "none"}]}
    ev.update(kw)
    return ev


def _scan_pages(n=2, failed=()):
    return {"scanned_count": n, "discovered": n, "failed": list(failed), "skipped": [], "stop_reason": None, "rows": [{"url": HOME, "scanned": True}, {"url": NOTICE, "scanned": True}][:n]}


def _consent(accept=True, reject=True, known=(0, 0, 0)):
    return {"pages_scanned": 2, "pages_found": 2, "consent": {"accept_tested": accept, "reject_tested": reject, "known": list(known)}}


def run(ev=None, policy=FULL_POLICY, sp=None, visual=None, mode="quick", **kw):
    ev = _evidence() if ev is None else ev
    return ds.assess_scanner(ev, copy.deepcopy(policy), sp or _scan_pages(), visual or _consent(), HOME, mode=mode, scanned_at="2026-11-05T10:00:00+00:00", **kw)


def P(r, ref):
    return next(p for p in r["parameters"] if p["ref"] == ref)


GA = {"id": "t1", "script_src": "https://www.google-analytics.com/g/collect", "vendor": "Google Analytics", "category": "analytics", "consent_states": ["pre_consent", "post_accept"]}
BANNER = [{"mechanism_type": "banner", "cmp_vendor": "CookieYes", "has_reject_all": True}]


# ── the matrix ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_matrix_is_a_hundred_points_and_a_quick_scan_reaches_seventy():
    assert sum(p[3] for p in ds.PARAMETERS) == 100
    assert sum(p[3] for p in ds.PARAMETERS if p[4] == "both") == 70
    by_domain = {d: sum(p[3] for p in ds.PARAMETERS if p[1] == d) for d in "ABCDEFG"}
    assert by_domain == {"A": 26, "B": 28, "C": 20, "D": 6, "E": 10, "F": 6, "G": 4}


def test_partial_credit_is_defined_in_whole_numbers_below_the_full_points():
    assert all(isinstance(v, int) and 0 < v < ds.POINTS[k] for k, v in ds.PARTIAL.items())


# ── the worked example of section 12 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_worked_example_no_banner_no_tracking_no_notice_scores_twenty_needs_attention():
    ev = _evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed", "http_status": 200}])
    r = ds.assess_scanner(ev, None, _scan_pages(1), _consent(), HOME, mode="quick", scanned_at="t")
    assert (P(r, "B1")["state"], P(r, "B2")["state"], P(r, "F1")["state"]) == ("earned",) * 3
    assert P(r, "A1")["state"] == "not_met" and P(r, "C1")["state"] == "prerequisite" and P(r, "C2")["state"] == "prerequisite"
    for ref in ("A2", "A3", "A4", "C3"):
        assert P(r, ref)["state"] == "prerequisite" and P(r, ref)["blocked_by"] == "A1"
    assert P(r, "B3")["state"] == "not_applicable" and P(r, "B4")["state"] == "not_applicable" and P(r, "G1")["state"] == "not_applicable"
    assert r["points"]["in_play"] == 57 and r["points"]["awarded"] == 16 and r["points"]["beyond_scan_type"] == 30
    assert r["score"] == 28 and r["scale"] == 100 and r["band"] == "Needs attention"      # 16 of 57 assessable points, out of 100
    # four parameters failing for one reason read as one finding with consequences (L-05)
    a1 = next(f for f in r["findings"] if f["ref"] == "A1")
    assert set(a1["consequences"]) == {"A2", "A3", "A4", "C1", "C2", "C3"}
    assert [f["ref"] for f in r["findings"]] == ["A1"]


def test_a_quick_scan_itemises_the_thirty_points_it_cannot_reach():
    r = run()
    assert sum(x["points"] for x in r["beyond_quick_scan"]) == 30
    assert {x["ref"] for x in r["beyond_quick_scan"]} == {"A5", "B5", "B6", "C4", "D1", "D2", "E1", "E2", "E3", "F2", "G2"}


# ── redistribution and the two reasons a point goes unawarded ───────────────────────────────────────────────────────────────────────
def test_every_score_is_out_of_one_hundred_and_a_quick_scan_is_held_at_99():
    r = run(ev=_evidence(consent_signals=BANNER), visual=_consent())
    assert r["score"] == 99 and r["score_text"] == "99/100" and r["band"] == "Evidence-led" and r["scale"] == 100
    assert any("held at 99" in n for n in r["band_notes"])
    assert "ceiling" not in r and "/70" not in str(r["score_text"]) and "70-point" not in r["explanation"]


def test_not_assessable_leaves_the_calculation_and_not_met_costs_points():
    blocked = run(sp=_scan_pages(2, failed=[{"url": "https://x.test/a", "reason": "Blocked by robots.txt"}]), policy=None, ev=_evidence(policies=[], pages=_pages()))
    assert P(blocked, "A1")["state"] in ("not_met", "not_assessable")
    r = run(policy={**FULL_POLICY, "requirements": [x for x in FULL_POLICY["requirements"] if x["id"] != "notice_purposes"] + [_req("notice_purposes", "not_found", "")]})
    assert P(r, "A3")["state"] == "not_met" and P(r, "A3")["awarded"] == 0
    assert r["points"]["in_play"] > r["points"]["awarded"]


def test_a_notice_that_could_not_be_read_is_our_limit_not_the_websites_gap():
    pol = {"status": "failed", "documents": [], "requirements": []}
    r = run(policy=pol)
    for ref in ("A2", "A3", "A4", "C1", "C2", "C3"):
        assert P(r, ref)["state"] == "not_assessable" and P(r, ref)["awarded"] is None


def test_a_page_blocked_by_the_site_is_restricted_and_confidence_is_low():
    r = run(sp=_scan_pages(2, failed=[{"url": "https://x.test/p", "reason": "Blocked by robots.txt"}]))
    assert r["status"] == "restricted" and r["confidence"]["level"] == "low"


# ── consent behaviour ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_analytics_before_any_choice_with_no_banner_fails_b1_and_blocks_b3_b4():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    assert P(r, "B1")["state"] == "not_met" and P(r, "B1")["outcome"] == ds.POTENTIAL_ISSUE
    assert P(r, "B2")["state"] == "not_met"
    assert P(r, "B3")["state"] == "prerequisite" and P(r, "B3")["blocked_by"] == "B1"      # SC-46: not exempt from the consent domain


def test_a_site_that_tracks_nothing_is_not_marked_down_for_having_no_banner():
    r = run()
    assert P(r, "B1")["state"] == "earned" and P(r, "B3")["state"] == "not_applicable"


def test_reject_not_honoured_is_a_potential_issue_and_reject_untested_is_not_a_failure():
    tracker_after_reject = {**GA, "consent_states": ["pre_consent", "post_accept", "post_reject"]}
    r = run(ev=_evidence(consent_signals=BANNER, trackers=[tracker_after_reject]), visual=_consent(known=(1, 1, 1)))
    assert P(r, "B4")["state"] == "not_met"
    r2 = run(ev=_evidence(consent_signals=BANNER), visual=_consent(reject=False))
    assert P(r2, "B4")["state"] == "not_assessable" and P(r2, "B4")["outcome"] == ds.NOT_TESTED and P(r2, "B4")["awarded"] is None      # L-03, GR-18


def test_no_reject_control_is_a_finding_while_reject_state_is_a_separate_not_tested():
    sig = [{"mechanism_type": "banner", "has_reject_all": False}]
    r = run(ev=_evidence(consent_signals=sig), visual=_consent(reject=False))
    assert P(r, "B3")["state"] == "not_met" and P(r, "B3")["outcome"] == ds.NOT_DETECTED
    assert P(r, "B4")["state"] == "not_assessable"


def test_deep_only_parameters_are_not_assessable_for_a_scan_made_before_their_evidence_was_recorded():
    r = run(ev=_evidence(consent_signals=BANNER), mode="deep")
    for ref in ("B5", "B6", "D1", "D2", "A5"):
        assert P(r, ref)["state"] == "not_assessable"
    assert r["coverage"] < 100 and r["score"] != 100


# ── the contents of the notice ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_complete_notice_earns_domains_a_and_c():
    r = run()
    for ref in ("A1", "A2", "A3", "A4", "C1", "C2", "C3"):
        assert P(r, ref)["state"] == "earned", ref


def test_a_grievance_contact_with_no_named_person_earns_c1_partially():
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] not in ("dpo_contact", "grievance_contact")] + [_req("grievance_contact", quote="Write to our grievance desk at help@x.test")]
    r = run(policy=pol)
    assert P(r, "C1")["state"] == "partial" and P(r, "C1")["awarded"] == ds.PARTIAL["C1"]


def test_a4_awards_by_how_many_of_the_three_routes_are_given():
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] != "consent_withdrawal"]
    r = run(policy=pol)
    assert P(r, "A4")["state"] == "partial" and P(r, "A4")["awarded"] == 3


# ── third parties, forms, security ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_deep_e1_and_e3_read_the_notice_against_what_the_scan_contacted():
    r = run(ev=_evidence(consent_signals=BANNER, trackers=[GA]), visual=_consent(known=(0, 1, 0)), mode="deep")
    assert P(r, "E1")["state"] == "earned" and P(r, "E3")["state"] == "earned"
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] != "data_sharing"] + [_req("data_sharing", quote="We share data with hosting providers.")]
    r2 = run(ev=_evidence(consent_signals=BANNER, trackers=[GA]), visual=_consent(known=(0, 1, 0)), policy=pol, mode="deep")
    assert P(r2, "E3")["state"] == "not_met"


def test_an_insecure_page_fails_f1():
    ev = _evidence(pages=_pages({"url": "http://x.test/old", "discovered_via": "link"}))
    sp = _scan_pages(3)
    sp["rows"].append({"url": "http://x.test/old", "scanned": True})
    r = run(ev=ev, sp=sp)
    assert P(r, "F1")["state"] == "not_met"


def test_forms_without_a_consent_control_are_not_scored_against_the_site_in_this_version():
    form = {"id": "f1", "page_url": HOME, "fields": [{"name": "email", "field_type": "email"}], "submit_url": "https://x.test/send"}
    r = run(ev=_evidence(forms=[form]))
    assert P(r, "G1")["state"] == "not_assessable"
    form2 = {**form, "fields": [{"name": "email", "field_type": "email"}, {"name": "agree", "field_type": "checkbox"}]}
    assert P(run(ev=_evidence(forms=[form2])), "G1")["state"] == "earned"


# ── bands and confidence ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fewer_than_sixty_percent_assessable_is_insufficient_evidence_whatever_it_scored():
    r = run(policy={"status": "failed", "documents": [], "requirements": []}, ev=_evidence(policies=[], pages=_pages()), sp=_scan_pages(0), visual={"pages_scanned": 0, "pages_found": 0, "consent": None})
    assert r["band"] == ds.INSUFFICIENT


def test_low_confidence_lowers_the_band_one_level_and_says_why():
    ok = run(ev=_evidence(consent_signals=BANNER), visual=_consent())
    low = run(ev=_evidence(consent_signals=BANNER), visual=_consent(), sp=_scan_pages(2, failed=[{"url": "https://x.test/p", "reason": "Blocked by robots.txt"}]))
    assert ds.BANDS.index(low["band"]) == ds.BANDS.index(ok["band"]) - 1
    assert any("confidence is low" in n for n in low["band_notes"])


def test_the_score_is_a_whole_number_and_nothing_prints_a_decimal():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    assert isinstance(r["score"], int)
    import json
    assert not __import__("re").search(r"\d\.\d", json.dumps({k: v for k, v in r.items() if k in ("score_text", "explanation", "findings", "top_findings")}))


def test_the_same_scan_gives_the_same_result_twice():
    import json
    a, b = run(ev=_evidence(trackers=[GA])), run(ev=_evidence(trackers=[GA]))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ── findings, remedies, wording ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_every_finding_carries_severity_and_confidence_separately_and_evidence():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    assert r["findings"]
    for f in r["findings"]:
        assert f["severity"] in ("High", "Medium", "Low") and f["confidence"] in ("High", "Medium", "Low") and f["evidence"] and f["owner"] and f["effort"]


def test_a_high_severity_finding_at_low_confidence_reads_as_something_to_investigate():
    ev = _evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed", "http_status": 200}])
    r = ds.assess_scanner(ev, None, _scan_pages(1, failed=[{"url": "https://x.test/p", "reason": "Blocked by robots.txt"}]), _consent(), HOME, mode="quick", scanned_at="t")
    a1 = next(f for f in r["findings"] if f["ref"] == "A1")
    assert a1["severity"] == "High" and a1["observed"].startswith("Worth investigating")


def test_remedies_are_only_for_evidenced_gaps_capped_and_do_not_name_unconfirmed_capabilities():
    ev = _evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed", "http_status": 200}])
    r = ds.assess_scanner(ev, None, _scan_pages(1), _consent(), HOME, mode="quick", scanned_at="t")
    assert len(r["remedies"]) <= ds.QUICK_REMEDY_CAP
    assert all(x["capability"] is None for x in r["remedies"])
    removed = {x["ref"] for x in r["removed"]}
    assert not removed & {x["ref"] for x in r["remedies"]}


def test_priority_actions_are_at_most_five_with_a_count_of_the_rest():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)), policy={"status": "analysed", "documents": FULL_POLICY["documents"], "requirements": [_req(i, "not_found", "") for i in ("notice_data_itemised", "notice_purposes", "consent_withdrawal", "rights_means", "grievance_contact", "dpo_contact")]})
    assert len(r["priority_actions"]) <= 5 and r["more_actions"] == max(0, len(r["findings"]) - 5)


# ── the validated-result gate ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_prohibited_wording_is_detected():
    for bad in ("This is a violation of the Act.", "The site is non-compliant", "Penalty applies", "an illegal practice", "The website is compliant", "personal data was sent to Google"):
        assert ds.prohibited_wording(bad), bad
    assert not ds.prohibited_wording("Analytics requests were observed before a choice was made.")


def test_every_phrase_in_the_library_and_the_disclaimer_is_free_of_prohibited_wording():
    for ref, parts in ds.PHRASES.items():
        for t in parts:
            assert not ds.prohibited_wording(t), (ref, t)
    assert not ds.prohibited_wording(run()["disclaimer"])


def test_the_gate_refuses_an_asserting_finding_with_no_evidence():
    r = run()
    r["parameters"][0]["evidence"] = []
    with pytest.raises(ds.ResultRefused):
        ds.validate_result(r)


def test_the_gate_refuses_a_score_on_an_outcome_that_asserts_nothing():
    r = run()
    p = next(p for p in r["parameters"] if p["state"] == "not_assessable")
    p["awarded"] = 0
    with pytest.raises(ds.ResultRefused):
        ds.validate_result(r)


def test_the_gate_refuses_a_prerequisite_block_that_names_no_blocker():
    r = run(ev=_evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed"}]), policy=None, sp=_scan_pages(1))
    p = next(p for p in r["parameters"] if p["state"] == "prerequisite")
    p["blocked_by"] = None
    with pytest.raises(ds.ResultRefused):
        ds.validate_result(r)


def test_the_gate_refuses_an_outcome_outside_the_permitted_list():
    r = run()
    r["parameters"][0]["outcome"] = "Compliant"
    with pytest.raises(ds.ResultRefused):
        ds.validate_result(r)


def test_a_quick_scan_can_never_issue_a_perfect_score():
    r = run(ev=_evidence(consent_signals=BANNER))
    r["score"] = 100
    with pytest.raises(ds.ResultRefused):
        ds.validate_result(r)


def test_every_earned_or_failed_parameter_has_evidence_naming_a_time():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    for p in r["parameters"]:
        if p["state"] in ("earned", "partial", "not_met"):
            assert p["evidence"] and all(e["at"] == "2026-11-05T10:00:00+00:00" for e in p["evidence"]), p["ref"]


def test_form_checks_read_the_evidence_api_field_names():
    form = {"id": "f1", "page_url": HOME, "fields": [{"name": "email", "field_type": "email"}, {"name": "agree", "field_type": "checkbox"}], "submit_url": "http://x.test/send"}
    r = run(ev=_evidence(forms=[form]), mode="deep")
    assert P(r, "F2")["state"] == "not_met" and P(r, "G1")["state"] == "earned"
    assert P(r, "G1")["evidence"][0]["page"] == HOME
    far = {**form, "submit_url": "https://crm.other.test/post"}
    assert P(run(ev=_evidence(forms=[far]), mode="deep"), "F2")["state"] == "partial"


def test_a_score_is_withheld_when_fewer_than_sixty_percent_could_be_assessed():
    r = run(ev=_evidence(policies=[], pages=_pages()), policy=None, sp=_scan_pages(2, failed=[{"url": "https://x.test/privacy", "reason": "Blocked by robots.txt"}]), mode="deep")
    assert r["band"] == ds.INSUFFICIENT and r["coverage"] < 60
    assert r["score"] is None and r["score_text"] == "No meaningful score" and r["indicative_score"] is not None
    assert r["status"] == "restricted" and r["confidence"]["level"] == "low"


def test_deep_scan_never_reaches_100_while_parameters_are_not_assessable_and_states_how_much_applied():
    r = run(ev=_evidence(consent_signals=BANNER, forms=[]), visual=_consent(), mode="deep")
    assert r["score"] == 99 and r["coverage"] < 100
    assert "parameters this scan type assesses applied" in r["explanation"]
    assert all(P(r, x)["state"] == "not_assessable" for x in ("A5", "B5", "B6", "D1", "D2"))


def test_deep_scan_scores_out_of_one_hundred_with_the_same_formula_as_quick():
    q, d = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0))), run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)), mode="deep")
    for r in (q, d):
        assert r["scale"] == 100 and r["score_text"].endswith("/100") and "ceiling" not in r
    assert d["points"]["reachable"] == 100 and q["points"]["reachable"] == 70


def test_a_consent_tool_whose_banner_was_never_shown_is_inconclusive_not_a_missing_banner():
    sig = [{"mechanism_type": "banner", "cmp_vendor": "CookieYes", "has_reject_all": None, "banner_displayed": False}]
    r = run(ev=_evidence(consent_signals=sig, trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    assert P(r, "B1")["state"] == "not_assessable" and P(r, "B1")["outcome"] == ds.INCONCLUSIVE and P(r, "B1")["awarded"] is None
