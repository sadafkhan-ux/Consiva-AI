"""The Deep Scan parameters the scanner could not test before: A5, B5, B6, D1, D2, E2, G1 (notice link) and G2 (Scanner Score, scoring/domain_score.py).

Each is judged from evidence the crawler now records (scanner/page_signals.py, scanner/withdrawal.py) and reads as "not assessable" when a scan was made before that evidence existed.
"""

import copy

from app.rules.vendor_orgs import org_of
from app.scanner.page_parser import ParsedLink, form_has_notice_link, parse_page
from app.scanner.page_signals import (
    child_directed_signal,
    links_to_privacy_notice,
    merge_child_surface,
    merge_notice_links,
)
from app.scoring import domain_score as ds
from tests.test_domain_score import (
    BANNER,
    FULL_POLICY,
    GA,
    HOME,
    P,
    _consent,
    _evidence,
    _req,
    run,
)

PX = {"id": "t9", "script_src": "https://px.adpixel.example/collect", "vendor": "Meta Pixel", "category": "marketing", "consent_states": ["pre_consent", "post_accept"], "page_url": "https://x.test/kids"}
SIGNALS = {
    "language_signals": {"pages_checked": 5, "pages_with_indic_script": 1, "indic_scripts": {"Devanagari": 400}, "switcher_scripts": [], "hreflang": ["en", "hi"], "html_lang": "en"},
    "notice_link_signals": {"pages": 5, "with_link": 5, "missing": []},
    "child_surfaces": [],
    "form_signals": [],
}
WITHDRAWN_OK = {"tested": True, "reason": None, "reopen_control": "Cookie settings", "withdraw_control": "Reject all", "accepted_non_essential": [{"vendor": "Google Analytics", "category": "analytics", "host": "g.test"}],
                "after_withdrawal": [], "further_pages": [{"url": "https://x.test/a", "loaded": True, "non_essential": []}, {"url": "https://x.test/b", "loaded": True, "non_essential": []}], "requests_only": True}


def banner_with(w):
    return [{**BANNER[0], "evidence": {"withdrawal": w} if w is not None else {}}]


def deep(**kw):
    sig = kw.pop("signals", SIGNALS)
    ev = kw.pop("ev", None) or _evidence(consent_signals=banner_with(None))
    return run(ev=ev, mode="deep", scan_signals=sig, **kw)


# ── A5 ──
def test_a5_earned_when_the_notice_is_linked_everywhere_and_another_language_is_offered():
    r = deep()
    assert P(r, "A5")["state"] == "earned" and P(r, "A5")["awarded"] == 3


def test_a5_partial_when_only_one_of_the_two_holds_and_failed_when_neither():
    one = {**SIGNALS, "notice_link_signals": {"pages": 5, "with_link": 3, "missing": ["https://x.test/landing"]}}
    assert P(deep(signals=one), "A5")["state"] == "partial" and P(deep(signals=one), "A5")["awarded"] == ds.PARTIAL["A5"]
    none = {**one, "language_signals": {"pages_checked": 5, "pages_with_indic_script": 0, "switcher_scripts": [], "hreflang": ["en"], "html_lang": "en"}}
    r = deep(signals=none)
    assert P(r, "A5")["state"] == "not_met" and "https://x.test/landing" in P(r, "A5")["evidence"][0]["page"]


def test_a5_a_notice_that_says_it_is_offered_in_an_indian_language_counts():
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"].append(_req("notice_language", quote="This notice is available in English and Hindi."))
    sig = {**SIGNALS, "language_signals": {"pages_checked": 5, "pages_with_indic_script": 0, "switcher_scripts": [], "hreflang": [], "html_lang": "en"}}
    assert P(deep(signals=sig, policy=pol), "A5")["state"] == "earned"


def test_a5_is_blocked_by_a_missing_notice_and_not_assessable_without_the_records():
    r = run(ev=_evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed"}]), policy=None, mode="deep", scan_signals=SIGNALS)
    assert P(r, "A5")["state"] == "prerequisite" and P(r, "A5")["blocked_by"] == "A1"
    assert P(run(mode="deep"), "A5")["state"] == "not_assessable"


# ── B5 and B6 ──
def test_b5_b6_earned_when_withdrawal_works_and_holds_on_two_further_pages():
    r = deep(ev=_evidence(consent_signals=banner_with(WITHDRAWN_OK)))
    assert P(r, "B5")["state"] == "earned" and P(r, "B6")["state"] == "earned"
    assert "Cookie settings" in P(r, "B5")["evidence"][0]["observed"] and "2 further pages" in P(r, "B6")["evidence"][0]["observed"]


def test_b5_fails_when_activity_continues_after_withdrawal_and_b6_when_it_returns_on_a_further_page():
    w = {**WITHDRAWN_OK, "after_withdrawal": [{"vendor": "Meta Pixel", "category": "marketing", "host": "px.test"}],
         "further_pages": [{"url": "https://x.test/a", "loaded": True, "non_essential": [{"vendor": "Meta Pixel", "category": "marketing", "host": "px.test"}]}, {"url": "https://x.test/b", "loaded": True, "non_essential": []}]}
    r = deep(ev=_evidence(consent_signals=banner_with(w)))
    assert P(r, "B5")["state"] == "not_met" and P(r, "B5")["outcome"] == ds.POTENTIAL_ISSUE and "Meta Pixel" in P(r, "B5")["found"]
    assert P(r, "B6")["state"] == "not_met" and "1 of 2 further pages" in P(r, "B6")["found"]


def test_no_reopen_control_is_a_finding_for_b5_and_not_tested_for_b6():
    w = {"tested": False, "reason": "no_reopen_control", "reopen_control": None, "withdraw_control": None, "accepted_non_essential": [], "after_withdrawal": [], "further_pages": []}
    r = deep(ev=_evidence(consent_signals=banner_with(w)))
    assert P(r, "B5")["state"] == "not_met" and P(r, "B5")["outcome"] == ds.NOT_DETECTED
    assert P(r, "B6")["state"] == "not_assessable" and P(r, "B6")["reason"] == ds.WITHDRAWAL_REASONS["no_reopen_control"]
    assert P(r, "B6")["awarded"] is None


def test_other_reasons_the_test_stopped_short_are_not_tested_never_failed():
    for reason in ("accept_not_clicked", "no_withdraw_control", "homepage_not_loaded", "error"):
        r = deep(ev=_evidence(consent_signals=banner_with({"tested": False, "reason": reason})))
        for ref in ("B5", "B6"):
            assert P(r, ref)["state"] == "not_assessable" and P(r, ref)["outcome"] == ds.NOT_TESTED, (reason, ref)
            assert P(r, ref)["reason"] == ds.WITHDRAWAL_REASONS[reason]


def test_b6_needs_two_further_pages_and_a_scan_without_the_test_says_so():
    w = {**WITHDRAWN_OK, "further_pages": [{"url": "https://x.test/a", "loaded": True, "non_essential": []}, {"url": "https://x.test/b", "loaded": False, "non_essential": []}]}
    assert P(deep(ev=_evidence(consent_signals=banner_with(w))), "B6")["state"] == "not_assessable"
    assert P(deep(), "B5")["reason"] == "Withdrawal was not tested in this scan."


def test_withdrawal_is_not_applicable_where_no_banner_is_needed():
    r = deep(ev=_evidence())
    assert P(r, "B5")["state"] == "not_applicable" and P(r, "B6")["state"] == "not_applicable"


def test_the_reason_texts_match_the_scanner_so_the_two_cannot_drift():
    from app.scanner.withdrawal import REASONS
    assert REASONS == ds.WITHDRAWAL_REASONS


# ── D1 and D2 ──
KIDS = {**SIGNALS, "child_surfaces": [{"url": "https://x.test/kids", "signal": "address contains “kids”"}]}


def test_d1_d2_are_not_applicable_when_no_page_is_aimed_at_children():
    r = deep()
    assert P(r, "D1")["state"] == "not_applicable" and P(r, "D2")["state"] == "not_applicable" and "declares itself" in P(r, "D1")["reason"]


def test_d1_fails_when_tracking_is_observed_on_a_child_page_and_d2_reads_the_notice():
    r = deep(ev=_evidence(consent_signals=BANNER, trackers=[PX]), signals=KIDS)
    assert P(r, "D1")["state"] == "not_met" and P(r, "D1")["outcome"] == ds.POTENTIAL_ISSUE
    assert P(r, "D2")["state"] == "not_met"
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"].append(_req("children_data", quote="We obtain verifiable parental consent before processing a child's data."))
    r2 = deep(ev=_evidence(consent_signals=BANNER, trackers=[{**PX, "page_url": "https://x.test/other"}]), signals=KIDS, policy=pol)
    assert P(r2, "D1")["state"] == "earned" and P(r2, "D2")["state"] == "earned"


def test_d1_d2_are_not_assessable_for_a_scan_that_never_looked_for_child_pages():
    sig = {k: v for k, v in SIGNALS.items() if k != "child_surfaces"}
    r = deep(signals=sig)
    assert P(r, "D1")["state"] == "not_assessable" and P(r, "D2")["state"] == "not_assessable"


def test_the_child_page_test_is_narrow():
    assert child_directed_signal("https://x.test/kids/games", "Games", "") == "address contains “kids”"
    assert child_directed_signal("https://x.test/a", "Home", "Fun for kids aged 5 - 9").startswith("text says")
    assert child_directed_signal("https://x.test/privacy", "Children's privacy", "") is None       # a policy is never a page for children
    assert child_directed_signal("https://x.test/about", "About us", "we insure families") is None
    acc = []
    merge_child_surface(acc, page_url="https://x.test/kids", signal="s")
    merge_child_surface(acc, page_url="https://x.test/other", signal=None)
    assert acc == [{"url": "https://x.test/kids", "signal": "s"}]


# ── E2 ──
def test_e2_reads_the_notice_when_a_service_is_established_outside_india():
    ev = _evidence(consent_signals=BANNER, trackers=[GA])
    assert P(deep(ev=ev, visual=_consent(known=(0, 1, 0))), "E2")["state"] == "not_met"
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"].append(_req("cross_border", quote="Your data may be processed outside India by our service providers."))
    r = deep(ev=ev, visual=_consent(known=(0, 1, 0)), policy=pol)
    assert P(r, "E2")["state"] == "earned" and "United States" in P(r, "E2")["evidence"][0]["observed"]


def test_e2_is_not_applicable_for_indian_or_no_services_and_not_assessable_for_unknown_ones():
    razor = {**GA, "vendor": "Razorpay", "category": "functional"}
    assert P(deep(ev=_evidence(consent_signals=BANNER, trackers=[razor])), "E2")["state"] == "not_applicable"
    assert P(deep(), "E2")["state"] == "not_applicable"
    mystery = {**GA, "vendor": "Unlisted Analytics Co"}
    assert P(deep(ev=_evidence(consent_signals=BANNER, trackers=[mystery])), "E2")["state"] == "not_assessable"


def test_the_vendor_table_states_where_the_organisation_is_established_and_never_guesses():
    assert org_of("Google Analytics").country == "United States" and org_of("Google Analytics").outside_india
    assert not org_of("Razorpay").outside_india
    assert org_of("No Such Vendor") is None and org_of(None) is None
    assert org_of("Hotjar").provenance == "research" and org_of("Hotjar").verified_on is None     # nothing is called verified until someone checks it (CI-05)


# ── G1 notice link and G2 ──
FORM = {"id": "f1", "selector": "form#c", "page_url": HOME, "fields": [{"name": "email", "field_type": "email", "checked": None}], "submit_url": "https://x.test/send"}
BOX = {"name": "agree", "field_type": "checkbox", "label": "I agree to the privacy policy", "checked": False}


def test_g1_counts_a_link_to_the_notice_beside_the_form():
    sig = {**SIGNALS, "form_signals": [{"page_url": HOME, "selector": "form#c", "notice_link": True}]}
    assert P(deep(ev=_evidence(forms=[FORM]), signals=sig), "G1")["state"] == "earned"
    nolink = {**SIGNALS, "form_signals": [{"page_url": HOME, "selector": "form#c", "notice_link": False}]}
    r = deep(ev=_evidence(forms=[FORM]), signals=nolink)
    assert P(r, "G1")["state"] == "not_met" and P(r, "G1")["outcome"] == ds.NOT_DETECTED


def test_g1_stays_inconclusive_for_a_scan_that_did_not_record_form_links():
    r = deep(ev=_evidence(forms=[FORM]), signals={k: v for k, v in SIGNALS.items() if k != "form_signals"})
    assert P(r, "G1")["state"] == "not_assessable"


def test_g2_fails_for_a_consent_box_that_starts_ticked_and_passes_otherwise():
    ticked = {**FORM, "fields": [FORM["fields"][0], {**BOX, "checked": True}]}
    r = deep(ev=_evidence(forms=[ticked]))
    assert P(r, "G2")["state"] == "not_met" and "already ticked" in P(r, "G2")["found"] and P(r, "G2")["outcome"] == ds.POTENTIAL_ISSUE
    clean = {**FORM, "fields": [FORM["fields"][0], BOX]}
    assert P(deep(ev=_evidence(forms=[clean])), "G2")["state"] == "earned"
    assert P(deep(ev=_evidence(forms=[FORM])), "G2")["state"] == "earned"      # no consent control at all: nothing is pre-selected (G1 is where its absence is judged)


def test_g2_is_not_assessable_when_the_state_was_not_recorded_and_ignores_unrelated_boxes():
    unknown = {**FORM, "fields": [FORM["fields"][0], {**BOX, "checked": None}]}
    assert P(deep(ev=_evidence(forms=[unknown])), "G2")["state"] == "not_assessable"
    other = {**FORM, "fields": [FORM["fields"][0], {"name": "remember", "field_type": "checkbox", "label": "Remember my size", "checked": True}]}
    assert P(deep(ev=_evidence(forms=[other])), "G2")["state"] == "earned"


# ── what the scanner records ──
def test_the_parser_reads_a_ticked_box_and_a_notice_link_beside_a_form():
    html = ('<html><body><div><form id="a"><input type="email" name="email"><label><input type="checkbox" name="c" checked> I agree</label></form>'
            '<p>See our <a href="/privacy-policy">Privacy Policy</a></p></div>'
            '<form id="b"><input type="email" name="email"><input type="checkbox" name="n"></form></body></html>')
    page = parse_page(html, "https://x.test/")
    a, b = page.forms
    assert [f.checked for f in a.fields] == [None, True] and a.notice_link is True
    assert [f.checked for f in b.fields] == [None, False] and b.notice_link is False
    live = parse_page(html, "https://x.test/", [[None, False], [None, True]])         # the browser's live state beats the markup
    assert [f.checked for f in live.forms[0].fields] == [None, False] and [f.checked for f in live.forms[1].fields] == [None, True]


def test_notice_link_signals_count_pages_and_name_the_pages_without_one():
    acc = {}
    merge_notice_links(acc, page_url="https://x.test/", has_link=links_to_privacy_notice([ParsedLink(href="https://x.test/privacy", text="Privacy Policy")]))
    merge_notice_links(acc, page_url="https://x.test/landing", has_link=links_to_privacy_notice([ParsedLink(href="https://x.test/about", text="About")]))
    assert acc == {"pages": 2, "with_link": 1, "missing": ["https://x.test/landing"]}
    assert form_has_notice_link(parse_page("<form><a href='/terms'>Terms</a></form>", "https://x.test/") and __import__("bs4").BeautifulSoup("<form><a href='/terms'>Terms</a></form>", "lxml").form)


def test_a_deep_scan_with_everything_covered_reaches_a_hundred_only_at_full_coverage():
    sig = {**SIGNALS, "form_signals": [{"page_url": HOME, "selector": "form#c", "notice_link": True}]}
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"].append(_req("cross_border", quote="Data may be processed outside India."))
    pol["requirements"].append(_req("children_data", quote="Verifiable parental consent is obtained."))
    forms = [{**FORM, "fields": [FORM["fields"][0], BOX]}]
    sig["child_surfaces"] = [{"url": "https://x.test/kids", "signal": "address contains “kids”"}]
    ev = _evidence(consent_signals=banner_with(WITHDRAWN_OK), forms=forms, trackers=[{**GA, "consent_states": ["post_accept"]}])
    r = run(ev=ev, policy=pol, mode="deep", scan_signals=sig, visual=_consent(known=(0, 1, 0)))
    states = {p["ref"]: p["state"] for p in r["parameters"]}
    assert all(v in ("earned", "not_applicable") for v in states.values()), {k: v for k, v in states.items() if v not in ("earned", "not_applicable")}
    assert r["coverage"] == 100
    assert r["score"] in (99, 100)        # 100 needs high confidence as well; a single-page consent test holds the report at medium


# ── the Act and Rules wording: A4 (s.5(1)(iii), r.3(c)), readiness indicators, statutory references ──
def _without(policy, *ids):
    pol = copy.deepcopy(policy)
    pol["requirements"] = [r for r in pol["requirements"] if r["id"] not in ids]
    return pol


def test_a4_asks_for_the_complaint_route_to_the_board_as_the_act_and_rules_say():
    assert P(run(), "A4")["state"] == "earned"
    r = run(policy=_without(FULL_POLICY, "complaint_to_board"))
    assert P(r, "A4")["state"] == "partial" and P(r, "A4")["awarded"] == 4 and "Board" in P(r, "A4")["found"]      # a grievance contact alone is the fiduciary's own route, half of that element
    r2 = run(policy=_without(FULL_POLICY, "complaint_to_board", "grievance_contact"))
    assert P(r2, "A4")["state"] == "partial" and P(r2, "A4")["awarded"] == 3
    r3 = run(policy=_without(FULL_POLICY, "complaint_to_board", "grievance_contact", "consent_withdrawal", "rights_means"))
    assert P(r3, "A4")["state"] == "not_met"


def test_indicators_are_observed_in_the_notice_and_never_carry_marks():
    r = run()
    ind = {i["id"]: i for i in r["indicators"]}
    assert [i["id"] for i in r["indicators"]] == ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9"]
    assert all(ind[k]["outcome"] == ds.OBSERVED and ind[k]["evidence"] and ind[k]["scored"] is False for k in ("I1", "I2", "I3", "I4", "I5", "I6", "I7"))
    assert "marks" not in ind["I1"] and "awarded" not in ind["I1"]


def test_an_indicator_that_the_notice_does_not_state_is_not_detected_and_does_not_move_the_score():
    full = run()
    bare = run(policy=_without(FULL_POLICY, "retention", "security", "breach_response", "cookies_tracking", "lawful_basis", "grievance_timeline", "complaint_to_board"))
    ind = {i["id"]: i for i in bare["indicators"]}
    assert ind["I1"]["outcome"] == ds.NOT_DETECTED and "retention" in ind["I1"]["found"].lower() and ind["I1"]["evidence"]
    assert bare["score"] <= full["score"] and all(p["state"] == q["state"] for p, q in zip(bare["parameters"], full["parameters"]) if p["ref"] not in ("A4", "C2"))


def test_indicators_are_blocked_without_a_notice_and_inconclusive_when_it_could_not_be_read():
    none = run(ev=_evidence(policies=[], pages=[{"url": HOME, "discovered_via": "seed"}]), policy=None, sp=_scan_pages_one())
    assert all(i["outcome"] == ds.BLOCKED for i in none["indicators"] if i["id"] not in ("I8", "I9"))
    unread = run(policy={"status": "failed", "documents": [], "requirements": []})
    assert all(i["outcome"] == ds.INCONCLUSIVE for i in unread["indicators"] if i["id"] not in ("I8", "I9"))


def test_the_home_page_security_headers_are_an_observation_not_a_verdict():
    base = {k: None for k in ("hsts", "csp", "x_frame_options", "x_content_type_options", "referrer_policy", "permissions_policy")}
    sent = deep(signals={**SIGNALS, "security_signals": {"homepage_headers": {**base, "hsts": "max-age=31536000", "x_content_type_options": "nosniff"}}})
    i8 = next(i for i in sent["indicators"] if i["id"] == "I8")
    assert i8["outcome"] == ds.OBSERVED and "2 of 6" in i8["found"] and "signal, not the safeguards" in i8["found"]
    none = deep(signals={**SIGNALS, "security_signals": {"homepage_headers": base}})
    assert next(i for i in none["indicators"] if i["id"] == "I8")["outcome"] == ds.NOT_DETECTED
    assert next(i for i in deep()["indicators"] if i["id"] == "I8")["outcome"] == ds.INCONCLUSIVE        # not recorded in this scan


def test_every_parameter_and_finding_names_its_provisions_with_relation_and_commencement():
    r = run(ev=_evidence(trackers=[GA]), visual=_consent(known=(1, 1, 0)))
    for p in r["parameters"]:
        assert p["statutory"] and all(s["provision"].startswith("DPDP ") and s["relation"] and s["commences"] == "2027-05-13" for s in p["statutory"]), p["ref"]
    f = r["findings"][0]
    assert f["statutory"] and all(s["in_force"] is False for s in f["statutory"])                       # scanned 5 Nov 2026: not yet in force
    later = ds.assess_scanner(_evidence(), copy.deepcopy(FULL_POLICY), _scan_pages_two(), _consent(), HOME, scanned_at="2027-06-01T00:00:00+00:00")
    assert all(s["in_force"] for p in later["parameters"] for s in p["statutory"])


def test_the_act_and_rules_references_in_the_domain_anchors_are_the_ones_checked():
    assert "5(3)" in ds.DOMAINS["A"]["anchor"] and "8(1)" in ds.DOMAINS["E"]["anchor"]


def _scan_pages_one():
    from tests.test_domain_score import _scan_pages
    return _scan_pages(1)


def _scan_pages_two():
    from tests.test_domain_score import _scan_pages
    return _scan_pages(2)


# ── choices by purpose (Functional Scope DS-12; indicator I9) ──
def banner_with_purposes(pu):
    return [{**BANNER[0], "evidence": {"purposes": pu} if pu is not None else {}}]


def i9(r):
    return next(i for i in r["indicators"] if i["id"] == "I9")


OK_PURPOSES = {"tested": True, "reason": None, "requests_only": True, "purposes": [
    {"kind": "analytics", "label": "Analytics cookies", "observed": [{"vendor": "Google Analytics", "category": "analytics", "host": "g.test"}], "verdict": "as_expected"},
    {"kind": "marketing", "label": "Marketing cookies", "observed": [{"vendor": "Meta Pixel", "category": "marketing", "host": "p.test"}], "verdict": "as_expected"}]}


def test_each_purpose_that_does_only_what_it_says_is_observed_and_carries_no_marks():
    r = deep(ev=_evidence(consent_signals=banner_with_purposes(OK_PURPOSES)))
    assert i9(r)["outcome"] == ds.OBSERVED and i9(r)["scored"] is False and "2 purposes tested independently" in i9(r)["found"] and i9(r)["evidence"]
    assert r["purposes"] == OK_PURPOSES                                  # shown beside the consent states
    base = deep(ev=_evidence(consent_signals=banner_with_purposes(None)))
    assert r["score"] == base["score"]                                   # an indicator never moves the score


def test_a_purpose_that_lets_other_tracking_through_is_a_potential_issue_naming_the_service():
    bad = {**OK_PURPOSES, "purposes": [{**OK_PURPOSES["purposes"][0], "observed": OK_PURPOSES["purposes"][0]["observed"] + OK_PURPOSES["purposes"][1]["observed"], "verdict": "unexpected"}, OK_PURPOSES["purposes"][1]]}
    r = deep(ev=_evidence(consent_signals=banner_with_purposes(bad)))
    assert i9(r)["outcome"] == ds.POTENTIAL_ISSUE and "Analytics cookies" in i9(r)["found"] and "Meta Pixel" in i9(r)["found"] and i9(r)["evidence"]


def test_choices_by_purpose_that_could_not_be_tested_say_why_and_are_never_failed():
    from app.scanner.purposes import REASONS
    assert REASONS == ds.PURPOSE_REASONS
    for reason in REASONS:
        r = deep(ev=_evidence(consent_signals=banner_with_purposes({"tested": False, "reason": reason, "purposes": [], "requests_only": True})))
        assert i9(r)["outcome"] == ds.NOT_TESTED and i9(r)["found"] == REASONS[reason] and not i9(r)["evidence"]
    assert i9(deep())["found"] == "Choices by purpose were not tested in this scan."
    assert i9(run(ev=_evidence(consent_signals=BANNER)))["found"] == "Choices by purpose are tested in a Deep Scan."      # Quick Scan
    assert i9(deep(ev=_evidence()))["outcome"] == ds.NOT_APPLICABLE


def test_c1_follows_the_act_and_rules_business_contact_information_not_a_name_or_a_title():
    pol = copy.deepcopy(FULL_POLICY)
    pol["requirements"] = [x for x in pol["requirements"] if x["id"] not in ("dpo_contact", "grievance_contact")] + [
        _req("dpo_contact", quote="Saurabh Kumar Swaran Soft Support Solutions Pvt. Ltd. Email: saurabhk@swaransoft.com")]
    r = run(policy=pol)
    assert P(r, "C1")["state"] == "earned" and P(r, "C1")["awarded"] == 6 and "Business contact information" in P(r, "C1")["found"]
    only_grievance = _without(FULL_POLICY, "dpo_contact")
    assert P(run(policy=only_grievance), "C1")["state"] == "partial"
    assert P(run(policy=_without(FULL_POLICY, "dpo_contact", "grievance_contact")), "C1")["state"] == "not_met"
