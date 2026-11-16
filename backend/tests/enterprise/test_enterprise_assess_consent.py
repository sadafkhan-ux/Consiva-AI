"""The consent parameters C4 to C8: each rule, at its boundaries, on plain observations."""
from app.enterprise.assess.consent import assess_consent
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import MET, NOT_ASSESSED, NOT_MET, PARTLY

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


def test_c7_no_reopen_control_is_not_met_only_with_low_confidence_so_a_person_decides():
    a = run(consent=docs(withdrawal={"tested": False, "reason": "no_reopen_control"}))["C7"]
    assert a.state == NOT_MET and a.confidence == "low"
    scored = eng.score([a], as_of=__import__("datetime").date(2027, 6, 1))
    line = scored.lines[[x.parameter_id for x in scored.lines].index("C7")]
    assert line.state == "human_review" and line.in_play == 0           # the engine sends a low-confidence reading to a person


# ---- C8 -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_c8_boundaries():
    base = {"tested": True, "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": [{"url": "u", "loaded": True, "non_essential": []}]}
    assert run(consent=docs(withdrawal=base))["C8"].state == MET
    assert run(consent=docs(withdrawal={**base, "after_withdrawal": [GA]}))["C8"].state == NOT_MET
    assert run(consent=docs(withdrawal={**base, "further_pages": [{"url": "u", "loaded": True, "non_essential": [META]}]}))["C8"].state == PARTLY


def test_c8_if_accept_started_nothing_there_was_nothing_to_stop_so_it_is_not_assessed_not_passed():
    r = run(consent=docs(withdrawal={"tested": True, "accepted_non_essential": [], "after_withdrawal": [], "further_pages": []}))["C8"]
    assert r.state == NOT_ASSESSED and "nothing for withdrawal to stop" in r.note


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
    assert r.points_in_play == 19 and r.points_earned == 19            # C4 5 + C5 5 + C7 5 + C8 3 + C6 1
    bad = docs(reject_persistence={"tested": True, "non_essential_after_reject": [GA], "further_pages": []}, button_comparison={"comparable": True, "why": "x"},
               withdrawal={"tested": True, "reopen_control": "a", "withdraw_control": "b", "accepted_non_essential": [GA], "after_withdrawal": [], "further_pages": []})
    assert eng.score(list(run(consent=bad).values()), as_of=datetime.date(2027, 6, 1)).points_earned == 14          # C5 not met loses its 5
