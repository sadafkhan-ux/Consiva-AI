"""Each text-parameter rule at its boundaries, on plain facts and observations. No model is called."""
import pytest

from app.enterprise.analysis import consolidate as co
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import HUMAN_REVIEW, MET, NEEDS_RECORDS, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY

from .test_enterprise_analysis_support import AS_OF, POLICY, ctx_from, ev, site


def base_rows(policy=POLICY, **kw):
    rows, _ = site(policy_text=policy, **kw)
    return rows


def run(facts=None, rows=None, **kw):
    c = ctx_from(rows or base_rows(), facts, **kw)
    return {a.parameter_id: a for a in co.assess_all(c)}, c


def fact(topic, value, quote, **kw):
    return {"topic": topic, "value": value, "quote": quote, **kw}


# ---- N1 ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_n1_met_when_a_policy_was_crawled_and_a_blog_about_privacy_is_not_it():
    r, _ = run()
    assert r["N1"].state == MET and r["N1"].evidence_refs and "privacy-policy" in r["N1"].note


def test_n1_not_met_when_only_a_blog_article_mentions_privacy():
    rows = [ev("page_text", "https://x.test/", "Home page text."), ev("page_text", "https://x.test/blog/privacy-policy-guide", "Posted on 1 May 2024. 5 min read. Our privacy policy guide. " * 30)]
    r = {a.parameter_id: a for a in co.assess_all(ctx_from(rows))}
    assert r["N1"].state == NOT_MET and r["N1"].evidence_refs and "none is a privacy notice" in r["N1"].note


def test_n1_not_assessed_with_no_text_or_an_unsettled_ambiguous_page():
    assert {a.parameter_id: a for a in co.assess_all(ctx_from([]))}["N1"].state == NOT_ASSESSED
    rows = [ev("page_text", "https://x.test/", "Home page text.")]
    assert {a.parameter_id: a for a in co.assess_all(ctx_from(rows, unresolved=["https://x.test/legal/info"]))}["N1"].state == NOT_ASSESSED


# ---- N7 ---------------------------------------------------------------------------------------------------------------------------------------------------------------

FRENCH = "Nous collectons votre nom et votre adresse électronique pour répondre à vos demandes et pour améliorer nos services. " * 20
HINDI = "हम आपका नाम और ईमेल पता एकत्र करते हैं। यह हमारी गोपनीयता नीति है और आपके अधिकार। " * 20


def test_n7_english_is_met():
    assert run()[0]["N7"].state == MET


def test_n7_eighth_schedule_script_is_met():
    assert run(rows=base_rows(HINDI))[0]["N7"].state == MET


def test_n7_other_language_without_an_option_is_not_met():
    r, _ = run(rows=base_rows(FRENCH))
    assert r["N7"].state == NOT_MET and r["N7"].evidence_refs


def test_n7_other_language_with_a_declared_english_option_is_met():
    rows = base_rows(FRENCH)
    rows.append(ev("header", "https://acme.test/privacy-policy", {"page_url": "https://acme.test/privacy-policy", "lang": "fr", "alternates": [{"lang": "en", "href": "https://acme.test/en/privacy"}]}))
    assert run(rows=rows)[0]["N7"].state == MET


def test_n7_other_language_with_a_stated_language_option_fact_is_met():
    r, _ = run([fact("language_option", "Hindi", "Cette politique est aussi disponible en Hindi.")], rows=base_rows(FRENCH))
    assert r["N7"].state == MET


# ---- G2: the 90-day rule ----------------------------------------------------------------------------------------------------------------------------------------------

def g2(period, mechanism=True):
    facts = []
    if mechanism:
        facts.append(fact("grievance_mechanism", "Grievance Officer", "Write to our Grievance Officer at g@acme.test."))
    if period:
        facts.append(fact("grievance_period", period, f"We respond within {period}."))
    return run(facts)[0]["G2"]


@pytest.mark.parametrize("period,state", [("30 days", MET), ("90 days", MET), ("91 days", NOT_MET), ("120 days", NOT_MET), ("3 months", MET), ("4 months", NOT_MET), ("48 hours", MET)])
def test_g2_period_boundaries(period, state):
    assert g2(period).state == state


def test_g2_not_met_note_names_the_limit_and_the_quote():
    a = g2("120 days")
    assert "120 days" in a.note and "90-day" in a.note and a.evidence_refs


def test_g2_mechanism_without_period_is_partly_met():
    assert g2(None).state == PARTLY and g2("a reasonable time").state == PARTLY


def test_g2_nothing_stated_is_not_met_when_the_policy_was_read_in_full():
    a = g2(None, mechanism=False)
    assert a.state == NOT_MET and a.confidence == "medium"


def test_g2_the_longest_period_counts():
    r, _ = run([fact("grievance_mechanism", "officer", "Write to our Grievance Officer."), fact("grievance_period", "2 days", "We acknowledge within 2 days."),
                fact("grievance_period", "120 days", "We resolve within 120 days.")])
    assert r["G2"].state == NOT_MET


# ---- failed or unread passages never become a finding -----------------------------------------------------------------------------------------------------------------

def test_absence_with_an_unread_passage_is_not_assessed_never_not_met():
    r, _ = run(unread={"privacy_policy": 2})
    for pid in ("G1", "G2", "G3", "G4", "G5", "G7", "G8", "N5", "N6", "N2", "N3"):
        assert r[pid].state == NOT_ASSESSED, pid
        assert "could not be analysed" in r[pid].note


def test_a_positive_finding_survives_an_unread_passage():
    r, _ = run([fact("board_complaint", "Board", "You may complain to the Data Protection Board of India.")], unread={"privacy_policy": 1})
    assert r["N6"].state == MET


def test_partly_met_with_an_unread_passage_becomes_not_assessed():
    r, _ = run([fact("grievance_mechanism", "officer", "Write to our Grievance Officer.")], unread={"privacy_policy": 1})
    assert r["G2"].state == NOT_ASSESSED


def test_without_a_policy_absence_is_not_assessed():
    rows = [ev("page_text", "https://x.test/", "Home page text.")]
    r = {a.parameter_id: a for a in co.assess_all(ctx_from(rows))}
    assert r["G1"].state == NOT_ASSESSED and r["N6"].state == NOT_ASSESSED and r["X2"].state == NOT_ASSESSED


# ---- statements, N5, N6, G1, G3 ---------------------------------------------------------------------------------------------------------------------------------------

def test_statement_only_parameters_met_when_stated_not_met_when_absent():
    r, _ = run([fact("retention", "12 months", "We retain your personal data for 12 months."), fact("security", "encryption", "We use encryption."),
                fact("breach", "notify", "We will notify you of a personal data breach.")])
    assert (r["G5"].state, r["G7"].state, r["G8"].state) == (MET, MET, MET)
    r, _ = run([])
    assert (r["G5"].state, r["G7"].state, r["G8"].state) == (NOT_MET, NOT_MET, NOT_MET)


def test_a_fact_with_a_topic_but_an_irrelevant_quote_does_not_count():
    r, _ = run([fact("breach", "x", "We love our customers dearly.")])
    assert r["G8"].state == NOT_MET


def test_n5_three_components():
    full = [fact("withdraw_consent", "settings", "You may withdraw your consent via https://x.test/settings."), fact("exercise_channel", "portal", "Use the portal at https://x.test/requests to exercise rights.")]
    assert run(full)[0]["N5"].state == MET
    no_link = [full[0], fact("exercise_channel", "email", "Write to privacy@x.test to exercise your rights.")]
    r = run(no_link)[0]["N5"]
    assert r.state == PARTLY and "link or portal: no" in r.note
    assert run([])[0]["N5"].state == NOT_MET


def test_n6_requires_the_board_to_be_named():
    assert run([fact("board_complaint", "Board", "You may complain to the Data Protection Board of India.")])[0]["N6"].state == MET
    assert run([fact("board_complaint", "regulator", "You may complain to the regulator.")])[0]["N6"].state == NOT_MET


def test_g1_needs_a_role_and_a_channel():
    assert run([fact("dpo_contact", "DPO", "Our Data Protection Officer can be reached at dpo@acme.test.")])[0]["G1"].state == MET
    assert run([fact("dpo_contact", "DPO", "We have a Data Protection Officer.")])[0]["G1"].state == NOT_MET


def test_g3_all_some_none():
    rights = [fact("right_access", "a", "You may access your data."), fact("right_correction", "c", "You may correct your data."), fact("right_erasure", "e", "You may erase your data."),
              fact("exercise_channel", "m", "Write to privacy@acme.test.")]
    assert run(rights)[0]["G3"].state == MET
    assert run(rights[:2])[0]["G3"].state == PARTLY
    assert run([])[0]["G3"].state == NOT_MET


def test_g4_nomination():
    assert run([fact("right_nominate", "n", "You may nominate another person.")])[0]["G4"].state == MET
    assert run([])[0]["G4"].state == NOT_MET


# ---- N2, N3, N4 (model interpretation, scripted) ----------------------------------------------------------------------------------------------------------------------

def J(verdict="yes", conf="high"):
    return {"verdict": verdict, "confidence": conf, "reason": "scripted", "fact_ids": [0]}


def items(n, topic="data_category", spec=True):
    return [fact(topic, f"item {i}", f"We collect item number {i} from you.", specific=spec) for i in range(n)]


def test_n2_boundaries():
    assert run(items(3), judgments={"N2": J()})[0]["N2"].state == MET
    assert run(items(2), judgments={"N2": J()})[0]["N2"].state == PARTLY
    assert run(items(3, spec=False), judgments={"N2": J("no")})[0]["N2"].state == NOT_MET
    assert run([], judgments={})[0]["N2"].state == NOT_MET


def test_n2_model_dissent_lowers_confidence_and_low_confidence_reaches_a_person():
    a = run(items(4), judgments={"N2": J("no", "high")})[0]["N2"]
    assert a.state == PARTLY and a.confidence == "low"
    res = eng.score([a], as_of=AS_OF)
    assert next(x for x in res.lines if x.parameter_id == "N2").state == HUMAN_REVIEW


def test_a_failed_interpretation_is_not_assessed_never_not_met():
    a = run(items(4), judgments={"N2": {"failed": "model down"}})[0]["N2"]
    assert a.state == NOT_ASSESSED and "failed" in a.note


def test_n3_needs_two_specific_purposes():
    assert run(items(2, "purpose"), judgments={"N3": J()})[0]["N3"].state == MET
    assert run(items(1, "purpose"), judgments={"N3": J()})[0]["N3"].state == PARTLY


def test_n4_is_capped_at_medium_and_a_middle_verdict_goes_to_a_person():
    a = run(judgments={"N4": J("yes", "high")})[0]["N4"]
    assert a.state == MET and a.confidence == "medium" and "confirm" in a.note
    assert run(judgments={"N4": J("partly")})[0]["N4"].state == HUMAN_REVIEW
    assert run(judgments={"N4": J("no")})[0]["N4"].state == NOT_MET
    assert run(judgments={})[0]["N4"].state == NOT_ASSESSED


def test_n4_not_assessed_for_a_policy_that_is_not_english():
    assert run(rows=base_rows(FRENCH), judgments={"N4": J()})[0]["N4"].state == NOT_ASSESSED


# ---- G6 ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_g6_named_recipient_is_met_and_generic_is_not_met():
    assert run([fact("sharing_recipient", "Google Analytics", "We share data with Google Analytics.")])[0]["G6"].state == MET
    r = run([fact("sharing_recipient", "partners", "We share data with our partners.", specific=False)])[0]["G6"]
    assert r.state == NOT_MET and "third-party host" in r.note


def test_g6_not_applicable_when_no_third_party_was_contacted_and_none_is_named():
    rows = [r for r in base_rows() if r.kind != "network"] + [ev("network", "https://acme.test/", {"page_url": "https://acme.test/", "hosts": [{"host": "acme.test", "third_party": False}], "non_essential": []})]
    assert run(rows=rows)[0]["G6"].state == NOT_APPLICABLE


# ---- K1 ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_k1_not_applicable_on_an_adult_b2b_site():
    r, _ = run()
    assert r["K1"].state == NOT_APPLICABLE and "no page, form or statement" in r["K1"].note


def test_k1_not_applicable_when_the_site_says_it_is_not_for_children():
    r, _ = run([fact("children_not_directed", "not for children", "Our services are not directed to children under 18.")])
    assert r["K1"].state == NOT_APPLICABLE


def test_k1_blog_posts_about_children_do_not_make_it_applicable():
    rows = base_rows() + [ev("page_text", "https://acme.test/blog/kids-and-data", "Posted on 1 May 2024. 4 min read. Children and data. " * 30)]
    assert run(rows=rows)[0]["K1"].state == NOT_APPLICABLE


def test_k1_a_kids_section_makes_it_applicable_and_missing_duties_are_not_met():
    rows = base_rows() + [ev("page_text", "https://acme.test/kids/games", "Games for kids. " * 30)]
    r = run(rows=rows)[0]["K1"]
    assert r.state == NOT_MET and r.confidence == "medium" and r.evidence_refs


def test_k1_both_duties_met_one_partly():
    rows = base_rows() + [ev("page_text", "https://acme.test/kids/games", "Games for kids. " * 30)]
    consent = fact("children_consent", "parental consent", "We obtain verifiable parental consent before collecting a child's data.")
    notrack = fact("children_no_tracking", "no ads", "We do not track or target advertising at children.")
    assert run([consent, notrack], rows=rows)[0]["K1"].state == MET
    assert run([consent], rows=rows)[0]["K1"].state == PARTLY


def test_k1_only_weak_signs_go_to_a_person():
    rows = base_rows() + [ev("page_text", "https://acme.test/schools/overview", "Software for schools. " * 30)]
    assert run(rows=rows)[0]["K1"].state == HUMAN_REVIEW


def test_k1_a_guardian_form_field_makes_it_applicable():
    rows = base_rows() + [ev("form", "https://acme.test/join", {"page_url": "https://acme.test/join", "forms": [{"fields": [{"type": "text", "name": "parent_name", "label": "Parent or guardian name"}]}]})]
    assert run(rows=rows)[0]["K1"].state == NOT_MET


# ---- M1, S1, T1, E1 ---------------------------------------------------------------------------------------------------------------------------------------------------

def test_m1_not_applicable_without_a_consent_manager_and_human_review_with():
    assert run()[0]["M1"].state == NOT_APPLICABLE
    assert run([fact("consent_manager", "XYZ", "You may manage consent through XYZ, a registered Consent Manager.")])[0]["M1"].state == HUMAN_REVIEW


def test_records_and_exemption_parameters():
    r, _ = run()
    assert r["S1"].state == NEEDS_RECORDS and r["T1"].state == NEEDS_RECORDS and r["E1"].state == HUMAN_REVIEW


# ---- X1, X2, X3 -------------------------------------------------------------------------------------------------------------------------------------------------------

def net(vendors, page="https://acme.test/"):
    return ev("network", page, {"page_url": page, "hosts": [{"host": h, "third_party": True} for _, h in vendors],
                                "non_essential": [{"vendor": v, "category": "analytics", "host": h} for v, h in vendors]})


def only_net(*vendors, policy=POLICY):
    rows = [r for r in base_rows(policy) if r.kind != "network"]
    return rows + [net(list(vendors))]


GA, HOTJAR, FB = ("Google Analytics", "www.google-analytics.com"), ("Hotjar", "static.hotjar.com"), ("Meta Pixel", "connect.facebook.net")


def test_x2_all_named_is_met():
    assert run(rows=only_net(GA))[0]["X2"].state == MET


def test_x2_an_undisclosed_tracker_is_not_met_and_the_host_is_named():
    r, _ = run(rows=only_net(HOTJAR))
    a = r["X2"]
    assert a.state == NOT_MET and "static.hotjar.com" in a.note and any(ref.startswith("ev:") for ref in a.evidence_refs)


def test_x2_most_named_but_one_missing_is_partly_met():
    pol = POLICY + "\nWe also use Meta for advertising and Amazon Web Services for hosting.\n"
    a = run(rows=only_net(GA, FB, HOTJAR, policy=pol))[0]["X2"]
    assert a.state == PARTLY and "Hotjar" in a.note


def test_x2_none_observed_is_met_and_no_policy_is_not_assessed():
    assert run(rows=only_net())[0]["X2"].state == MET
    rows = [ev("page_text", "https://x.test/", "Home page.")]
    assert {a.parameter_id: a for a in co.assess_all(ctx_from(rows))}["X2"].state == NOT_ASSESSED


def cookies(*names, third=()):
    cs = [{"identity": f"{n}|acme.test|/", "name": n, "domain": "acme.test", "first_party": True} for n in names]
    cs += [{"identity": f"{n}|{d}|/", "name": n, "domain": d, "first_party": False} for n, d in third]
    return ev("cookie", "https://acme.test/", {"page_url": "https://acme.test/", "cookies": cs})


def only_cookies(c, policy=POLICY):
    return [r for r in base_rows(policy) if r.kind != "cookie"] + [c]


def test_x1_boundaries():
    pol = POLICY + "\nWe set the _ga cookie and the sid cookie.\n"
    assert run(rows=only_cookies(cookies("_ga", "sid"), pol))[0]["X1"].state == MET
    assert run(rows=only_cookies(cookies("_ga", "sid", "zz1"), pol))[0]["X1"].state == PARTLY
    a = run(rows=only_cookies(cookies("_ga", "qq9", "zz1", "yy2"), pol))[0]["X1"]
    assert a.state == NOT_MET and "zz1" in a.note
    assert run(rows=[r for r in base_rows() if r.kind != "cookie"])[0]["X1"].state == NOT_APPLICABLE


def test_x1_a_third_party_cookie_is_declared_when_its_service_is_named():
    pol = POLICY + "\n"
    assert run(rows=only_cookies(cookies(third=[("IDE", "doubleclick.net")]), pol + "Google may set advertising cookies."))[0]["X1"].state == MET
    assert run(rows=only_cookies(cookies(third=[("fr", "facebook.com")]), pol))[0]["X1"].state == NOT_MET


def form_rows(collects):
    f = {"index": 0, "fields": [{"type": "text", "name": "x"}], "collects": collects, "privacy_link": True, "text": "", "label_text": ""}
    return [r for r in base_rows() if r.kind != "form"] + [ev("form", "https://acme.test/contact", {"page_url": "https://acme.test/contact", "forms": [f]})]


def test_x3_boundaries():
    assert run(rows=form_rows(["email", "name", "phone"]))[0]["X3"].state == MET
    assert run(rows=form_rows(["email", "name", "date of birth"]))[0]["X3"].state == PARTLY
    a = run(rows=form_rows(["date of birth", "company"]))[0]["X3"]
    assert a.state == PARTLY or a.state == NOT_MET
    pol = "Privacy Policy\n\nWe collect your email.\n"
    a = run(rows=[r for r in form_rows(["date of birth", "address", "password", "email"]) if r.kind != "page_text" or "privacy" not in r.source_url] + [ev("page_text", "https://acme.test/privacy-policy", pol)])[0]["X3"]
    assert a.state == NOT_MET and "date of birth" in a.note
    assert run(rows=[r for r in base_rows() if r.kind != "form"])[0]["X3"].state == NOT_APPLICABLE


# ---- N8, C1, C2, C3, C9 -----------------------------------------------------------------------------------------------------------------------------------------------

def form(**kw):
    f = {"index": 0, "fields": [{"type": "email", "name": "email", "checked": False}], "collects": ["email"], "consent_controls": [], "preticked": [], "privacy_link": False, "text": "", "label_text": ""}
    f.update(kw)
    return f


def frows(*forms, url="https://acme.test/contact"):
    return [r for r in base_rows() if r.kind != "form"] + [ev("form", url, {"page_url": url, "forms": list(forms)})]


def test_n8_boundaries():
    assert run(rows=frows(form(privacy_link=True)))[0]["N8"].state == MET
    assert run(rows=frows(form(label_text="By joining you accept our privacy notice")))[0]["N8"].state == MET
    a = run(rows=frows(form(privacy_link=True), form(index=1)))[0]["N8"]
    assert a.state == NOT_MET and "1 of 2" in a.note
    assert run(rows=[r for r in base_rows() if r.kind != "form"])[0]["N8"].state == NOT_ASSESSED
    assert run(rows=frows(form(collects=["email", "password"])))[0]["N8"].state == NOT_ASSESSED          # a login form is not a collection


def box(label="Send me offers", checked=False):
    return {"type": "checkbox", "name": "offers", "label": label, "checked": checked}


def test_c3_preticked_is_not_met():
    a = run(rows=frows(form(fields=[{"type": "email", "name": "email", "checked": False}, box(checked=True)], consent_controls=[box(checked=True)], preticked=[box(checked=True)])))[0]["C3"]
    assert a.state == NOT_MET and a.confidence == "high" and "pre-ticked" in a.note


def test_c3_unticked_control_is_met_and_bundling_or_by_submitting_is_partly():
    ok = form(fields=[box()], consent_controls=[box()])
    assert run(rows=frows(ok))[0]["C3"].state == MET
    bundled = form(fields=[box("I agree to the terms, the privacy policy, marketing offers and sharing with third parties")], consent_controls=[box()])
    assert run(rows=frows(bundled))[0]["C3"].state == PARTLY
    submit = form(text="By submitting you agree to our terms.", fields=[{"type": "email", "name": "email", "checked": False}])
    assert run(rows=frows(ok, submit, ))[0]["C3"].state == PARTLY


def test_c3_not_assessed_without_a_control_or_without_the_checked_field():
    assert run(rows=frows(form()))[0]["C3"].state == NOT_ASSESSED
    old = form(fields=[{"type": "checkbox", "name": "agree", "label": "I agree"}], consent_controls=[{"type": "checkbox"}])
    a = run(rows=frows(old))[0]["C3"]
    assert a.state == NOT_ASSESSED and "predates" in a.note


def test_c2_needs_a_named_contact_in_the_request():
    good = form(consent_controls=[box()], label_text="Contact our Data Protection Officer at dpo@acme.test")
    bad = form(consent_controls=[box()], label_text="I agree")
    assert run(rows=frows(good))[0]["C2"].state == MET
    assert run(rows=frows(bad))[0]["C2"].state == NOT_MET
    assert run(rows=[r for r in base_rows() if r.kind != "form"])[0]["C2"].state == NOT_ASSESSED


def test_c1_components():
    req = form(consent_controls=[box()], label_text="I agree to receive offers")
    assert run(rows=frows(req), judgments={"C1": J()})[0]["C1"].state == MET
    assert run(rows=frows(req), judgments={"C1": J("no")})[0]["C1"].state == PARTLY
    fr = form(consent_controls=[box()], label_text="Je consens à recevoir des offres de la société et de ses partenaires commerciaux selon les conditions")
    assert run(rows=frows(fr), judgments={"C1": J("no")})[0]["C1"].state == NOT_MET
    assert run(rows=frows(req), judgments={"C1": {"failed": "x"}})[0]["C1"].state == NOT_ASSESSED


def c9_judgment(*items, failed=0):
    forms = [("e", "https://acme.test/join", {"fields": [{"label": "Email", "name": "email"}, {"label": "Date of birth", "name": "dob"}]})]
    return {"C9": {"batches": [{"forms": forms * max(len(items), 1), "result": {"items": [dict(index=i, **it) for i, it in enumerate(items)]} if not failed else {"failed": "x"}}]}}


def test_c9_boundaries_and_invented_excess_fields_are_ignored():
    ok = dict(necessary="yes", excess_fields=[], reason="r", confidence="high")
    bad = dict(necessary="no", excess_fields=["Date of birth"], reason="r", confidence="high")
    ghost = dict(necessary="no", excess_fields=["Blood group"], reason="r", confidence="high")
    assert run(judgments=c9_judgment(ok, ok))[0]["C9"].state == MET
    assert run(judgments=c9_judgment(ok, ok, bad))[0]["C9"].state == PARTLY
    a = run(judgments=c9_judgment(ok, bad))[0]["C9"]
    assert a.state == NOT_MET and a.confidence == "medium"
    assert run(judgments=c9_judgment(ghost))[0]["C9"].state == MET
    assert run(judgments=c9_judgment(failed=1))[0]["C9"].state == NOT_ASSESSED


# ---- observations and the engine ---------------------------------------------------------------------------------------------------------------------------------------

def test_observations_list_undisclosed_trackers_forms_without_notice_and_old_policies():
    rows = only_net(HOTJAR) + frows(form())[-1:]
    c = ctx_from(rows, [fact("policy_date", "1 March 2021", "Last updated 1 March 2021")])
    o = co.observations(c)
    assert o["undisclosed_trackers"] == ["hotjar"] and o["forms_without_notice_link"] and o["possibly_outdated"] == [{"url": "https://acme.test/privacy-policy", "date": "2021-03-01"}]


def test_every_assessment_is_accepted_by_the_scoring_engine():
    r, _ = run([fact("board_complaint", "Board", "You may complain to the Data Protection Board.")])
    res = eng.score(list(r.values()), as_of=AS_OF)
    assert {x.parameter_id for x in res.lines} >= set(r)


def test_only_catalogue_parameters_are_assessed_and_none_is_a_technical_or_cookie_consent_one():
    r, _ = run()
    assert not {"TS1", "TS2", "TS3", "TS4", "TS5", "TS6", "C4", "C5", "C6", "C7", "C8"} & set(r)
    assert len(r) == 28


def test_met_not_met_always_carry_evidence_refs():
    r, _ = run([fact("board_complaint", "Board", "You may complain to the Data Protection Board.")])
    for a in r.values():
        if a.state in (MET, PARTLY, NOT_MET):
            assert a.evidence_refs, a.parameter_id


def test_a_page_full_of_menu_labels_is_still_english_when_its_prose_is():
    from app.enterprise.analysis import textrules as tr
    menu = "\n".join(["Agentic AI", "Services", "Industries", "Work", "About", "Contact", "Case Studies", "Blog", "info@x.com", "+91 9220 313 650"] * 40)
    prose = "\n".join(["We collect the personal data that you provide to us when you fill in a form on our website, and we use it to respond to your enquiry."] * 30)
    assert tr.detect_language(menu + "\n" + prose)["language"] == "en"
    assert tr.detect_language("हम आपकी व्यक्तिगत जानकारी एकत्र करते हैं और उसका उपयोग करते हैं " * 20)["language"] == "eighth_schedule"


def test_when_no_item_is_marked_specific_but_the_model_reads_some_as_concrete_a_person_decides_not_not_met():
    for verdict in ("yes", "partly"):
        a = run(items(5, "purpose", spec=False), judgments={"N3": J(verdict)})[0]["N3"]
        assert a.state == HUMAN_REVIEW and a.confidence == "low" and "a person should decide" in a.note
    assert run(items(5, "purpose", spec=False), judgments={"N3": J("no")})[0]["N3"].state == NOT_MET          # both agree it is not specific


def test_a_policy_made_mostly_of_lists_and_tables_is_still_english_but_french_is_not():
    from app.enterprise.analysis import textrules as tr
    table = "\n".join(["_ga Google Analytics 2 years analytics cookie used to distinguish users and measure visits to this website for statistics"] * 60)
    assert 0.06 < tr.detect_language(table)["english_ratio"] < 0.2 or tr.detect_language(table)["language"] == "en"
    assert tr.detect_language(table)["language"] == "en"
    french = "\n".join(["Nous collectons les données personnelles que vous nous fournissez lorsque vous remplissez un formulaire sur notre site afin de répondre à votre demande."] * 30)
    assert tr.detect_language(french)["language"] == "other"
