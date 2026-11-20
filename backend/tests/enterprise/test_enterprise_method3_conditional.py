"""Method 3.0.1 (owner's approval of 20 Nov 2026): the conditional duties apply by website category, the same way for every site.

K01-K05 (s.9, Rules 10-12), RT4 (s.8(8), Rule 8, Third Schedule) and G02 (s.8(9), Rule 9). Applicable / Not applicable / Human review; Human review is held out of the maximum
and listed apart; severity is the duty's, never the category's.
"""
import json

import pytest

from app.enterprise.analysis.repo import EvRow, FrontierRow
from app.enterprise.method3 import catalogue as cat
from app.enterprise.method3.assess import assess
from app.enterprise.method3.context import assemble
from app.enterprise.method3.engine import FAIL, NA, PARTIAL, PASS, MethodError, Result, score
from app.enterprise.method3.site_category import ECOMMERCE, EDTECH, GAMING, GENERAL, detect, users_stated
from app.enterprise.report.method3 import M3ReportData, render_html
from tests.enterprise.test_enterprise_method3 import AS_OF, SITE, all_results, rows


def site(*, pages=(), home_text=None, children_excluded=True, forms=(), hosts=(), html=None):
    """The synthetic shop of test_enterprise_method3, with extra pages, home text, forms and third-party hosts."""
    ev, fr, chunks = rows()
    if not children_excluded:
        chunks[0].result["facts"] = [f for f in chunks[0].result["facts"] if f["topic"] != "children_not_directed"]
        ev[0] = EvRow("1", "page_text", SITE + "privacy-policy", 0, "f1", "rendered_text", ev[0].content.replace("7. Children. Our services are not directed at children.", ""))
    if home_text is not None:
        ev = [EvRow(r.id, r.kind, r.source_url, r.page_number, r.frontier_id, r.detection_method, home_text) if r.id == "5" else r for r in ev]
    for i, (path, text) in enumerate(pages):
        u = SITE + path
        fr.append(FrontierRow(f"x{i}", u, u, "done", "text/html"))
        ev.append(EvRow(f"xt{i}", "page_text", u, 0, f"x{i}", "rendered_text", text))
        ev.append(EvRow(f"xn{i}", "network", u, 0, f"x{i}", "browser_requests", json.dumps({"page_url": u, "consent_state": "pre_consent", "hosts": list(hosts), "non_essential": [],
                                                                                         "insecure_requests": 0})))
    if forms:
        u = SITE + "signup"
        fr.append(FrontierRow("fs", u, u, "done", "text/html"))
        ev.append(EvRow("fsn", "network", u, 0, "fs", "browser_requests", json.dumps({"page_url": u, "consent_state": "pre_consent", "hosts": [], "non_essential": []})))
        ev.append(EvRow("fsf", "form", u, 0, "fs", "dom_forms", json.dumps({"page_url": u, "forms": [{"index": 0, "page_url": u, "action": u, "method": "post",
                                                                                                    "fields": [{"type": "text", "name": n, "label": n} for n in forms]}]})))
    if html is not None:
        ev = [EvRow(r.id, r.kind, r.source_url, r.page_number, r.frontier_id, r.detection_method, html) if r.id == "3" else r for r in ev]
    m = assemble(ev, fr, chunks, scan_id="s", root_url=SITE, as_of=AS_OF)
    return m, {r.parameter_id: r for r in assess(m, [])}


RAZORPAY = {"host": "api.razorpay.com", "vendor": "Razorpay", "third_party": True}


# ---- category detection -----------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_plain_business_site_is_general():
    m, _ = site()
    assert detect(m).primary == GENERAL


def test_ecommerce_needs_a_shop_page_or_its_own_statement_not_just_a_payment_gateway():
    m, _ = site(pages=[("pricing", "Plans and pricing. Pay securely.")], hosts=[RAZORPAY])
    assert not detect(m).has(ECOMMERCE)
    m, _ = site(pages=[("cart", "Your cart. Add to cart. Checkout")], hosts=[RAZORPAY])
    assert detect(m).has(ECOMMERCE)


def test_a_blog_post_about_other_companies_is_not_a_self_description():
    m, _ = site(pages=[("blog/dpdp-guide", "We are an online marketplace? No: an e-commerce entity with two crore users must erase data after three years.")])
    assert not detect(m).has(ECOMMERCE)


def test_user_numbers_are_read_in_indian_and_international_units():
    assert [n for n, _ in users_stated("Trusted by 2.5 crore users and 50 lakh+ players, 3 million customers")] == [25_000_000, 5_000_000, 3_000_000]
    assert users_stated("Contact 3 users") == []


# ---- RT4 ----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_rt4_does_not_apply_to_an_ordinary_site():
    _, r = site()
    assert r["RT4"].status == NA and r["RT4"].applicability == cat.NOT_APPLICABLE and "Not a Third Schedule platform" in r["RT4"].explanation


def test_rt4_is_human_review_when_the_platform_size_is_not_shown():
    _, r = site(home_text="We are an online marketplace for tea.", pages=[("cart", "Your cart. Add to cart.")], hosts=[RAZORPAY])
    x = r["RT4"]
    assert x.status == NA and x.applicability == cat.HUMAN_REVIEW and x.review and "registered users in India" in x.explanation
    out = score(list(r.values()), as_of=AS_OF)
    assert out["human_review_pending"] == ["RT4"] and out["human_review_max"] == cat.BY_ID["RT4"].max_marks
    line = next(x for x in out["lines"] if x["parameter_id"] == "RT4")
    assert line["earned"] is None and line["severity"] is None


def test_rt4_applies_above_the_threshold_and_a_silent_policy_fails_with_its_severity():
    _, r = site(home_text="We are an online marketplace for tea, trusted by 3 crore customers.", pages=[("cart", "Your cart. Add to cart.")], hosts=[RAZORPAY])
    x = r["RT4"]
    assert x.applicability == cat.APPLICABLE and x.status == FAIL and x.review
    assert cat.severity_of("RT4", x.status) == "medium"


def test_gaming_uses_its_own_fifty_lakh_threshold():
    _, r = site(home_text="We are an online gaming platform with 60 lakh players.", pages=[("play", "Play now. Fantasy cricket contests.")])
    assert r["RT4"].applicability == cat.APPLICABLE
    _, r = site(home_text="We are an online gaming platform with 40 lakh players.", pages=[("play", "Play now. Fantasy cricket contests.")])
    assert r["RT4"].applicability == cat.HUMAN_REVIEW            # "40 lakh" does not prove fewer registered users in India: the organisation confirms


# ---- children -----------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_no_evidence_of_children_is_not_applicable_with_the_reason():
    _, r = site(children_excluded=False)
    assert all(r[p].applicability == cat.NOT_APPLICABLE and "No evidence of children's data" in r[p].explanation for p in ("K01", "K02", "K03", "K04"))


def test_an_edtech_site_is_not_assumed_to_serve_children():
    m, r = site(children_excluded=False, home_text="We are an online learning platform. Learners enrol in courses.", pages=[("courses", "Online courses. Enrol now.")])
    assert detect(m).has(EDTECH)
    assert all(r[p].applicability == cat.HUMAN_REVIEW and r[p].status == NA for p in ("K01", "K02", "K03", "K04"))
    _, r = site(home_text="We are an online learning platform. Learners enrol in courses.", pages=[("courses", "Online courses. Enrol now.")])
    assert r["K01"].applicability == cat.NOT_APPLICABLE                  # the policy excludes children and nothing contradicts it


def test_a_parent_field_makes_the_children_duties_apply_and_a_silent_policy_fails_high():
    _, r = site(children_excluded=False, forms=["Parent name", "Child's name", "Class"])
    assert r["K01"].applicability == cat.APPLICABLE and r["K01"].status == FAIL and cat.severity_of("K01", FAIL) == "high"
    assert r["K01"].evidence[0].startswith("Applies because a form asks for a parent or guardian")


def test_a_parent_field_against_a_policy_that_excludes_children_goes_to_human_review():
    _, r = site(forms=["Parent name", "Child's name"])
    assert r["K01"].applicability == cat.HUMAN_REVIEW and "disagrees" in r["K01"].explanation


def test_k05_needs_more_than_a_policy_clause_that_restates_the_law():
    _, r = site(pages=[("about", "For a person with disability, consent is given by the lawful guardian.")])
    assert r["K05"].applicability == cat.NOT_APPLICABLE


# ---- G02 ----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_g02_applies_to_every_site_and_checks_the_link_opens_the_address_shown():
    _, r = site()
    assert r["G02"].applicability == cat.APPLICABLE and "every reply" in (r["G02"].limitation or "")
    _, r = site(html='<html><body><a href="/privacy-policy">Privacy</a><footer><a href="mailto:sales@other.example">grievance@shop.example.in</a></footer></body></html>')
    assert any(u.status == PARTIAL and "different address" in u.note for u in r["G02"].urls)


def test_g02_is_general_whatever_the_category():
    _, r = site(home_text="We are an online gaming platform.", pages=[("play", "Play now. Fantasy cricket.")])
    assert r["G02"].applicability == cat.APPLICABLE


# ---- engine and report -------------------------------------------------------------------------------------------------------------------------------------------------------

def test_severity_is_the_duty_s_and_lower_for_partial():
    assert cat.severity_of("K03", FAIL) == "high" and cat.severity_of("K03", PARTIAL) == "medium" and cat.severity_of("G02", PARTIAL) == "low"
    assert cat.severity_of("K01", PASS) is None and cat.severity_of("K01", NA) is None and cat.severity_of("N01", FAIL) is None


def test_human_review_must_hold_the_marks_back_and_be_flagged():
    res = all_results()
    res = [Result("K01", PASS, "x", ["e"], applicability=cat.HUMAN_REVIEW, review=True) if x.parameter_id == "K01" else x for x in res]
    with pytest.raises(MethodError):
        score(res, as_of=AS_OF)
    res = [Result("K01", NA, "reason", applicability=cat.APPLICABLE) if x.parameter_id == "K01" else x for x in res]
    with pytest.raises(MethodError):
        score(res, as_of=AS_OF)


def test_existing_results_without_applicability_still_score_the_same():
    out = score(all_results(), as_of=AS_OF)
    assert out["score"] == 100 and out["human_review_pending"] == [] and all(x["applicability"] in (cat.APPLICABLE, cat.NOT_APPLICABLE) for x in out["lines"])


def test_report_shows_category_applicability_severity_and_held_back_marks():
    m, r = site(home_text="We are an online marketplace for tea.", pages=[("cart", "Your cart. Add to cart.")], hosts=[RAZORPAY])
    out = score(list(r.values()), as_of=AS_OF)
    out["site_category"] = detect(m).to_dict()
    d = M3ReportData("s" * 8, "o", SITE, {"started_at": None, "completed_at": None}, out, "r", pages=[{"url": SITE, "category": "other", "forms": 0}])
    html = render_html(d)
    for want in ("Website category: E-commerce", "Applicability, severity and status", "Human review", "marks held back", "Third Schedule"):
        assert want in html, want
