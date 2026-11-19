"""The consent parameters C4 to C8: each rule, at its boundaries, on plain observations."""
from app.enterprise.assess.consent import assess_consent
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import MET, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY

GA = {"vendor": "Google Analytics", "category": "analytics", "host": "www.google-analytics.com"}
META = {"vendor": "Meta Pixel", "category": "marketing", "host": "connect.facebook.net"}


def net(i, firing=()):
    return (f"n{i}", {"page_url": f"https://x.test/{i}", "non_essential": list(firing)})


def cookie_ev(i, *names, state="pre_consent"):
    return (f"c{i}", {"cookies": [{"name": n, "domain": "x.test", "consent_state": state} for n in names]})


def classify(name, domain):
    return {"_ga": "analytics", "_fbp": "marketing", "sid": "functional"}.get(name)


def docs(**kw):
    return {k: (f"t-{k}", {"test": k, **v}) for k, v in kw.items()}


def run(networks=None, cookies=(), consent=None):
    r = assess_consent(networks=[net(1), net(2)] if networks is None else networks, cookies=list(cookies), tests=consent or {}, classify=classify)
    return {a.parameter_id: a for a in r}


# ---- C4 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c4_met_when_no_page_fired_and_no_such_cookie_was_set():
    r = run(cookies=[cookie_ev(1, "sid")])["C4"]
    assert r.state == MET and r.evidence_refs and "none of 2 pages" in r.note


def test_c4_not_met_when_any_page_contacted_analytics_before_a_choice():
    r = run(networks=[net(1), net(2, [GA])])["C4"]
    assert r.state == NOT_MET and "1 of 2 pages" in r.note and "Google Analytics" in r.note and r.evidence_refs == ("ev:n2",)


def test_c4_not_met_for_an_analytics_cookie_set_before_a_choice_even_with_no_request():
    r = run(cookies=[cookie_ev(1, "_ga", "sid")])["C4"]
    assert r.state == NOT_MET and "_ga" in r.note and "sid" not in r.note


def test_c4_a_cookie_seen_after_a_choice_is_not_a_cookie_before_it():
    assert run(cookies=[cookie_ev(1, "_ga", state="post_accept")])["C4"].state == MET


def test_c4_nothing_to_look_at_or_evidence_before_classification_is_not_assessed_not_passed():
    assert run(networks=[])["C4"].state == NOT_ASSESSED
    assert run(networks=[("n", {"page_url": "https://x.test/"})])["C4"].state == NOT_ASSESSED


# ---- C5 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c5_boundaries():
    clean = {"tested": True, "non_essential_after_reject": [], "further_pages": [{"url": "u", "loaded": True, "non_essential": []}]}
    assert run(consent=docs(reject_persistence=clean))["C5"].state == MET
    assert run(consent=docs(reject_persistence={**clean, "non_essential_after_reject": [GA]}))["C5"].state == NOT_MET
    later = {**clean, "further_pages": [{"url": "u", "loaded": True, "non_essential": [META]}]}
    r = run(consent=docs(reject_persistence=later))["C5"]
    assert r.state == PARTLY and "Meta Pixel" in r.note


def test_c5_reject_that_could_not_be_given_is_not_assessed_and_names_why():
    r = run(consent=docs(reject_persistence={"tested": False, "reason": "reject_not_clicked"}))["C5"]
    assert r.state == NOT_ASSESSED and "Reject could not be given" in r.note
    assert run(consent={})["C5"].state == NOT_ASSESSED


# ---- C6 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c6_comparable_or_not_and_missing_when_a_control_was_not_found():
    assert run(consent=docs(button_comparison={"comparable": True, "why": "same size"}))["C6"].state == MET
    r = run(consent=docs(button_comparison={"comparable": False, "why": "Reject is a text link"}))["C6"]
    assert r.state == NOT_MET and r.note == "Reject is a text link"
    assert run(consent=docs(button_comparison={"tested": False, "reason": "banner_or_controls_not_found"}))["C6"].state == NOT_ASSESSED


# ---- C7 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c7_boundaries():
    assert run(consent=docs(withdrawal={"tested": True, "reopen_control": "Cookie settings", "withdraw_control": "Reject all"}))["C7"].state == MET
    assert run(consent=docs(withdrawal={"tested": False, "reason": "no_withdraw_control", "reopen_control": "Cookie settings"}))["C7"].state == PARTLY
    for reason in ("accept_not_clicked", "homepage_not_loaded", "error"):
        assert run(consent=docs(withdrawal={"tested": False, "reason": reason}))["C7"].state == NOT_ASSESSED


def test_c7_no_reopen_control_is_not_met_with_low_confidence_so_a_reviewer_confirms():
    a = run(consent=docs(withdrawal={"tested": False, "reason": "no_reopen_control"}))["C7"]
    assert a.state == NOT_MET and a.confidence == "low"
    scored = eng.score([a], as_of=__import__("datetime").date(2027, 6, 1))
    line = scored.lines[[x.parameter_id for x in scored.lines].index("C7")]
    assert line.state == NOT_MET and line.status == "Fail" and "reviewer" in line.adjustment        # method version 2: the result stands, marked for a reviewer


# ---- C8 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c8_boundaries():
    base = {"tested": True, "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": [{"url": "u", "loaded": True, "non_essential": []}]}
    assert run(consent=docs(withdrawal=base))["C8"].state == MET
    assert run(consent=docs(withdrawal={**base, "after_withdrawal": [GA]}))["C8"].state == NOT_MET
    assert run(consent=docs(withdrawal={**base, "further_pages": [{"url": "u", "loaded": True, "non_essential": [META]}]}))["C8"].state == PARTLY


def test_c8_if_accept_started_nothing_there_was_nothing_to_stop_so_it_does_not_apply():
    r = run(consent=docs(withdrawal={"tested": True, "accepted_non_essential": [], "after_withdrawal": [], "further_pages": []}))["C8"]
    assert r.state == NOT_APPLICABLE and "nothing for withdrawal to stop" in r.note and r.evidence_refs


# ---- C10, C11 and the site with no banner -------------------------------------------------------------------------------------------------------------------------

BANNER = {"accept": {"tested": True, "non_essential": [GA]}, "reject": {"tested": True, "non_essential": []}}
NO_BANNER = {"accept": {"tested": False, "reason": "control_not_found"}, "reject": {"tested": False, "reason": "control_not_found"}}


def test_c10_met_when_a_banner_was_found():
    a = run(consent=docs(**BANNER))["C10"]
    assert a.state == MET and a.evidence_refs


def test_c10_not_met_when_analytics_runs_and_no_banner_exists():
    a = run(networks=[net(1, [GA])], consent=docs(**NO_BANNER))["C10"]
    assert a.state == NOT_MET and "Google Analytics" in a.note and a.evidence_refs == ("ev:n1",)


def test_with_nothing_that_needs_consent_and_no_banner_the_consent_duties_do_not_apply():
    r = run(networks=[net(1), net(2)], consent=docs(**NO_BANNER))
    assert {p: r[p].state for p in ("C5", "C6", "C7", "C8", "C10", "C11")} == {p: NOT_APPLICABLE for p in ("C5", "C6", "C7", "C8", "C10", "C11")}


def test_with_analytics_and_no_banner_refusal_and_withdrawal_fail_on_what_ran():
    r = run(networks=[net(1, [META])], consent=docs(**NO_BANNER))
    assert {p: r[p].state for p in ("C5", "C7", "C8", "C10", "C11")} == {p: NOT_MET for p in ("C5", "C7", "C8", "C10", "C11")}
    assert all(r[p].evidence_refs for p in ("C5", "C7", "C8")) and r["C6"].state == NOT_ASSESSED      # C6 cannot compare buttons that do not exist; C10 records the missing banner


def test_when_the_entry_page_did_not_load_whether_a_banner_exists_could_not_be_verified():
    r = run(consent=docs(accept={"tested": False, "reason": "page_not_loaded"}, reject={"tested": False, "reason": "page_not_loaded"}))
    assert r["C10"].state == NOT_ASSESSED and r["C11"].state == NOT_ASSESSED


def test_c11_per_purpose_choices_pass_and_a_banner_without_them_partly_passes():
    full = run(consent=docs(**BANNER, purposes={"tested": True, "purposes": [{"kind": "analytics", "label": "Analytics"}, {"kind": "marketing", "label": "Marketing"}]}))["C11"]
    assert full.state == MET and "Analytics" in full.note
    part = run(consent=docs(**BANNER, purposes={"tested": False, "reason": "no_preferences_control"}))["C11"]
    assert part.state == PARTLY and "no preferences control" in part.note


def test_c8_untested_withdrawal_is_not_assessed():
    assert run(consent=docs(withdrawal={"tested": False, "reason": "no_reopen_control"}))["C8"].state == NOT_ASSESSED


# ---- all ----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_every_result_that_earns_or_loses_points_names_its_evidence():
    full = docs(reject_persistence={"tested": True, "non_essential_after_reject": [], "further_pages": []}, button_comparison={"comparable": True, "why": "x"},
                 withdrawal={"tested": True, "reopen_control": "a", "withdraw_control": "b", "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": []})
    for a in run(consent=full).values():
        assert a.state == NOT_ASSESSED or a.evidence_refs, a.parameter_id


def test_the_assessments_flow_into_the_scoring_engine_with_the_approved_points():
    import datetime
    good = docs(reject_persistence={"tested": True, "non_essential_after_reject": [], "further_pages": []}, button_comparison={"comparable": True, "why": "x"},
                withdrawal={"tested": True, "reopen_control": "a", "withdraw_control": "b", "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": []})
    r = eng.score(list(run(consent=good).values()), as_of=datetime.date(2027, 6, 1))
    assert r.points_earned == 14            # method version 2: C4 4 + C5 4 + C7 3 + C8 2 + C6 1 (C10 and C11 were not tested here, so they Fail and earn nothing)
    bad = docs(reject_persistence={"tested": True, "non_essential_after_reject": [GA], "further_pages": []}, button_comparison={"comparable": True, "why": "x"},
               withdrawal={"tested": True, "reopen_control": "a", "withdraw_control": "b", "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": []})
    assert eng.score(list(run(consent=bad).values()), as_of=datetime.date(2027, 6, 1)).points_earned == 10          # C5 Fail loses its 4
