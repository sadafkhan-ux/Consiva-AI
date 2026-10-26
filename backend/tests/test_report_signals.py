"""The additions behind the report's new sections: language and security observations, the cookie inventory's purposes and lifetimes, contact details
found beside a policy quotation, and what fixing each gap does to the score."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db.repositories import scan_repository
from app.policy.analysis import PolicyDocument, PolicyExtraction, PolicyReqResult, analyse, extract_contacts
from app.rules.cookie_purposes import LONG_LIVED_DAYS, UNKNOWN_PURPOSE, describe_cookie, lifetime_days
from app.scanner import page_signals as ps
from app.scanner.page_parser import parse_page
from app.scoring import dpdp_score

HINDI = "नमस्ते यह हमारी वेबसाइट है"
TAMIL = "வணக்கம்"


# ── language observations ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_indian_script_text_is_counted_by_script():
    counts = ps.indic_script_counts(f"Welcome {HINDI} and {TAMIL}")
    assert counts["Devanagari (Hindi, Marathi, Sanskrit, Nepali, Konkani, Maithili, Dogri, Bodo)"] == len(HINDI.replace(" ", ""))
    assert counts["Tamil"] == len(TAMIL)
    assert ps.indic_script_counts("Plain English only, with numbers 123 and symbols ©®") == {}
    assert ps.indic_script_counts("") == {} and ps.indic_script_counts(None) == {}


def test_arabic_script_is_not_counted_as_an_indian_script():
    assert ps.indic_script_counts("مرحبا بكم") == {}


def test_the_page_declares_its_language_and_its_alternates():
    page = parse_page('<html lang="en-IN"><head><link rel="alternate" hreflang="hi" href="/hi"><link rel="alternate" hreflang="ta" href="/ta">'
                      '<link rel="stylesheet" href="/x.css"></head><body>hello</body></html>', "https://x.test/")
    assert page.html_lang == "en-IN" and page.hreflang == ["hi", "ta"]
    bare = parse_page("<html><head></head><body>hello</body></html>", "https://x.test/")
    assert bare.html_lang is None and bare.hreflang == []


def test_language_observations_add_up_across_pages_and_only_the_homepage_names_the_language():
    acc: dict = {}
    ps.merge_language(acc, indic={"Tamil": 5}, control_indic={}, html_lang="en", hreflang=["hi"], is_seed=True)
    ps.merge_language(acc, indic={}, control_indic={"Tamil": 2}, html_lang="fr", hreflang=["hi", "ta"], is_seed=False)
    ps.merge_language(acc, indic={"Tamil": 3}, control_indic={}, html_lang=None, hreflang=[], is_seed=False)
    assert acc == {"pages_checked": 3, "pages_with_indic_script": 2, "indic_scripts": {"Tamil": 8}, "switcher_scripts": ["Tamil"], "hreflang": ["hi", "ta"], "html_lang": "en"}


def test_the_hreflang_list_is_bounded():
    acc: dict = {}
    ps.merge_language(acc, indic={}, control_indic={}, html_lang=None, hreflang=[f"x{i}" for i in range(200)], is_seed=True)
    assert len(acc["hreflang"]) == ps.MAX_HREFLANG


# ── security observations ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_safeguard_headers_are_read_whatever_their_case_and_missing_ones_are_none():
    h = ps.security_headers({"Strict-Transport-Security": "max-age=31536000", "CONTENT-SECURITY-POLICY": "default-src 'self'", "X-Frame-Options": "DENY", "Server": "nginx"})
    assert h == {"hsts": "max-age=31536000", "csp": "default-src 'self'", "x_frame_options": "DENY", "x_content_type_options": None, "referrer_policy": None, "permissions_policy": None}


def test_a_long_header_value_is_cut_and_nothing_else_about_the_response_is_kept():
    h = ps.security_headers({"content-security-policy": "x" * 5000, "set-cookie": "secret=1", "authorization": "x"})
    assert len(h["csp"]) == 300 and "set-cookie" not in str(h) and "secret" not in str(h)


def test_https_is_counted_per_page_and_the_headers_are_the_homepages():
    acc: dict = {}
    ps.merge_security(acc, final_url="https://x.test/", headers={"strict-transport-security": "max-age=1"}, is_seed=True)
    ps.merge_security(acc, final_url="http://x.test/old", headers={"x-frame-options": "DENY"}, is_seed=False)
    assert acc["pages_total"] == 2 and acc["pages_https"] == 1 and acc["homepage_https"] is True
    assert acc["homepage_headers"]["hsts"] == "max-age=1" and acc["homepage_headers"]["x_frame_options"] is None   # the second page's headers are not the homepage's


def test_reading_headers_never_raises():
    assert ps.response_headers(SimpleNamespace(headers={"A": "b"})) == {"A": "b"}
    assert ps.response_headers(SimpleNamespace()) == {}
    assert ps.response_headers(SimpleNamespace(headers=object())) == {}
    assert ps.response_headers(None) == {}


# ── cookie purposes and lifetimes ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,provider", [
    ("_ga", "Google Analytics"), ("_ga_KYXX0TPK2E", "Google Analytics"), ("_gcl_au", "Google Ads"), ("_fbp", "Meta (Facebook)"), ("fr", "Meta (Facebook)"),
    ("MUID", "Microsoft"), ("CLID", "Microsoft Clarity"), ("_clck", "Microsoft Clarity"), ("__cf_bm", "Cloudflare"), ("_cfuvid", "Cloudflare"),
    ("_hjSessionUser_123", "Hotjar"), ("hubspotutk", "HubSpot"), ("PHPSESSID", "The website"), ("csrftoken", "The website"),
    ("OptanonConsent", "OneTrust"), ("cmp_consent", "Consent tool"), ("woocommerce_cart_hash", "WooCommerce"),
])
def test_a_recognised_cookie_gets_its_providers_description(name, provider):
    d = describe_cookie(name, None)
    assert d["provider"] == provider and d["purpose_source"] == "catalogue" and d["purpose"].endswith(".") and len(d["purpose"]) > 30


def test_an_unrecognised_cookie_gets_an_honest_sentence_never_an_invented_purpose():
    d = describe_cookie("xq9_zz_unknown", None)
    assert d == {"provider": None, "purpose": UNKNOWN_PURPOSE, "purpose_source": "unknown"}
    c = describe_cookie("xq9_zz_unknown", "analytics")
    assert c["purpose_source"] == "category" and "analytics" in c["purpose"] and "could not be identified" in c["purpose"] and c["provider"] is None


def test_a_session_cookie_has_no_lifetime_and_a_persistent_one_is_measured_from_when_it_was_first_seen():
    seen = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert lifetime_days(None, seen.isoformat()) is None
    assert lifetime_days(seen + timedelta(days=400), seen.isoformat()) == 400.0
    assert lifetime_days(seen + timedelta(days=400), seen.isoformat()) > LONG_LIVED_DAYS
    assert lifetime_days(seen + timedelta(minutes=29), seen.isoformat()) == pytest.approx(29 / 1440, abs=1e-3)
    assert lifetime_days(seen + timedelta(days=3), None) is None                       # when it was first seen was not recorded
    assert lifetime_days(seen + timedelta(days=3), "garbage") is None
    assert lifetime_days(seen - timedelta(days=1), seen.isoformat()) == 0.0              # already expired: not negative


def _cookie(**kw):
    base = dict(name="_ga", category="analytics", vendor="Google Analytics", path="/", expiry=None, secure=False, http_only=False, same_site="Lax", observations=[])
    return SimpleNamespace(**{**base, **kw})


def test_the_cookie_inventory_carries_purpose_provider_and_lifetime():
    seen = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    d = scan_repository._cookie_detail(_cookie(expiry=seen + timedelta(days=730), observations=[{"observed_at": seen.isoformat(), "consent_state": "pre_consent"}]))
    assert d["purpose_source"] == "catalogue" and d["provider"] == "Google Analytics" and d["lifetime_days"] == 730.0 and d["is_session"] is False
    s = scan_repository._cookie_detail(_cookie(name="whatever_x", category=None))
    assert s["is_session"] is True and s["lifetime_days"] is None and s["purpose_source"] == "unknown"


def test_the_agents_own_evidence_is_unchanged_by_the_report_additions():
    form = SimpleNamespace(id="f", selector="form", fields=[], purpose_guess=None, submit_url="https://x.test/send", page_id="p")
    page = SimpleNamespace(id="p", url="https://x.test/", title="t", http_status=200, discovered_via="seed")
    cookie = SimpleNamespace(id="c", name="_ga", domain="x.test", category="analytics", vendor="Google Analytics", is_first_party=True, source="rule",
                             consent_states=["pre_consent"], path="/", expiry=None, secure=False, http_only=False, same_site="Lax", observations=[])
    plain = scan_repository._serialize_evidence([page], [form], [cookie], [], [], [], [])
    assert "submit_url" not in plain["forms"][0] and "page_url" not in plain["forms"][0] and "purpose" not in plain["cookies"][0] and "lifetime_days" not in plain["cookies"][0]
    detail = scan_repository._serialize_evidence([page], [form], [cookie], [], [], [], [], include_cookie_detail=True)
    assert detail["forms"][0]["submit_url"] == "https://x.test/send" and detail["forms"][0]["page_url"] == "https://x.test/" and detail["cookies"][0]["purpose"]


# ── contacts beside a policy quotation ──────────────────────────────────────────────────────────────────────────────────────────

def test_contacts_are_read_as_written_and_listed_once():
    c = extract_contacts("Write to Saurabh.K@Swaransoft.com or saurabh.k@swaransoft.com, call +91 9818 600 650, or use https://x.test/grievance-form. Ref 12345.")
    assert c["emails"] == ["Saurabh.K@Swaransoft.com", "saurabh.k@swaransoft.com"]      # exactly as written
    assert c["phones"] == ["+91 9818 600 650"] and c["urls"] == ["https://x.test/grievance-form"]
    assert extract_contacts("nothing here") == {"emails": [], "phones": [], "urls": []}
    assert extract_contacts("call 12345") ["phones"] == []                                # too short to be a phone number


POLICY = ("Our Grievance Officer is Saurabh K, Privacy Officer. Email: Saurabhk@Swaransoft.com. Phone: +91 9818 600 650. "
          "We will acknowledge your grievance within 48 hours and resolve it within 30 days. " + "Filler text. " * 40)


def _row(req_id, quote):
    doc = PolicyDocument("https://x.test/privacy", "privacy_policy", POLICY, "crawl")
    out = analyse(PolicyExtraction(results=[PolicyReqResult(id=req_id, found=True, quote=quote, document=1)]), [doc], None, False)
    return next(r for r in out["requirements"] if r["id"] == req_id)


def test_the_grievance_contact_row_carries_the_details_found_beside_the_quotation():
    row = _row("grievance_contact", "Our Grievance Officer is Saurabh K, Privacy Officer.")
    assert row["contacts"]["emails"] == ["Saurabhk@Swaransoft.com"] and row["contacts"]["phones"] == ["+91 9818 600 650"]


def test_the_timeline_row_carries_the_period_in_days_and_other_rows_carry_neither():
    row = _row("grievance_timeline", "We will acknowledge your grievance within 48 hours and resolve it within 30 days.")
    assert row["response_days"] == 30
    other = _row("retention", "We will acknowledge your grievance within 48 hours and resolve it within 30 days.")
    assert other["contacts"] is None and other["response_days"] is None


def test_a_point_that_was_not_found_has_no_contacts():
    doc = PolicyDocument("https://x.test/privacy", "privacy_policy", POLICY, "crawl")
    out = analyse(PolicyExtraction(results=[]), [doc], None, False)
    # A row the model did not offer and the page-text pattern did not find has nothing to carry. (A row the pattern did find is not "not found": it has its own quotation.)
    still_missing = [r for r in out["requirements"] if r["status"] == "not_found"]
    assert still_missing and all(r["contacts"] is None and r["response_days"] is None and not r["quote"] for r in still_missing)
    assert all(r.get("found_by") == "pattern" and r["quote"] for r in out["requirements"] if r["status"] in ("verified", "partial"))


# ── what fixing each gap does to the score ──────────────────────────────────────────────────────────────────────────────────────────

def _evidence(pre_trackers=True):
    trackers = [{"id": "t", "script_src": "https://www.googletagmanager.com/gtm.js", "vendor": "Google Tag Manager", "category": "analytics", "source": "rule",
                 "consent_states": ["pre_consent"] if pre_trackers else ["post_accept"], "page_url": "https://x.test/"}]
    return {"pages": [{"id": "p", "url": "https://x.test/", "title": "Home", "http_status": 200, "discovered_via": "seed"}], "forms": [], "cookies": [], "trackers": trackers,
            "policies": [], "third_party_services": [], "consent_signals": [{"mechanism_type": "cmp", "cmp_vendor": "X", "has_reject_all": True, "has_granular_choices": True,
                                                                              "evidence": {"accept_interaction": "clicked", "reject_interaction": "clicked", "accept_click_confirmed": True, "reject_click_confirmed": True}}]}


def test_each_remediation_step_says_what_fixing_it_does_to_the_same_score():
    s = dpdp_score.assess(_evidence())
    r = s.readiness
    assert s.remediation_plan, "the evidence has a gap"
    for step in s.remediation_plan:
        res = next(x for x in s.requirements if x.id == step.requirement_id)
        regained = res.weight * (1.0 if step.outcome == "Not Met" else 0.5)
        assert step.current_percent == r.score_percent                                              # the number the report shows now
        assert step.percent_if_fixed == pytest.approx(round((r.points_earned + regained) / r.assessed_weight * 100, 1), abs=0.05)
        assert step.gain_percent == pytest.approx(step.percent_if_fixed - step.current_percent, abs=0.11)
        assert step.gain_percent > 0 and step.percent_if_fixed <= 100


def test_a_partly_met_gap_regains_half_of_what_a_not_met_gap_of_the_same_weight_would():
    s = dpdp_score.assess(_evidence())
    by = {x.requirement_id: x for x in s.remediation_plan}
    weights = {x.id: x.weight for x in s.requirements}
    for rid, step in by.items():
        factor = 1.0 if step.outcome == "Not Met" else 0.5
        assert step.gain_percent == pytest.approx(weights[rid] * factor / s.readiness.assessed_weight * 100, abs=0.11)


def test_the_impact_is_the_documented_one_or_empty_never_invented():
    s = dpdp_score.assess(_evidence())
    from app.scoring.findings import BUSINESS_IMPACT
    for step in s.remediation_plan:
        assert step.impact == BUSINESS_IMPACT.get(step.requirement_id, "")


def test_no_impact_figures_are_given_when_the_score_is_withheld():
    s = dpdp_score.assess({"pages": [], "forms": [], "cookies": [], "trackers": [], "policies": [], "third_party_services": [], "consent_signals": []})
    assert s.readiness.status != "scored" and all(p.gain_percent is None and p.percent_if_fixed is None for p in s.remediation_plan)


def test_a_clean_scan_has_nothing_to_fix_and_the_score_is_untouched_by_the_plan():
    s = dpdp_score.assess(_evidence(pre_trackers=False))
    before = s.readiness.score_percent
    assert all(p.gain_percent is not None for p in s.remediation_plan)
    assert dpdp_score.assess(_evidence(pre_trackers=False)).readiness.score_percent == before
