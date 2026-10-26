"""Privacy-policy content analysis: exact quotations only, checked legal mappings, fail-soft, and never touching the score."""

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import consent_scans
from app.main import app
from app.policy import analysis as pa
from app.policy.analysis import (NOT_APPLICABLE, NOT_FOUND, PARTIAL, VERIFIED, PolicyDocument, PolicyExtraction, PolicyReqResult, analyse,
                                 collect_documents, has_contact, response_days, verify_quote)
from app.policy.requirements import BY_ID, CHILD_SECTORS, GROUPS, REQUIREMENTS
from app.scanner.policy_detector import POLICY_TEXT_LIMIT, detect_policies
from app.scanner.page_parser import ParsedLink

POLICY = (
    "Swaran Soft Support Solutions Pvt. Ltd. (‘Swaran Soft’) collects personal data you provide directly — your name, email address, phone number, "
    "company name and job title — when you fill out a contact form. We use the information we collect to respond to your inquiries, send you requested "
    "resources and comply with legal obligations. Under the Digital Personal Data Protection Act, 2023, you have the right to access your personal data; "
    "correct inaccurate data; erase your personal data where it is no longer necessary; withdraw consent at any time; nominate another individual; "
    "and file a grievance with us or with the Data Protection Board of India. Grievance Officer: Saurabh Kumar, Email: saurabhk@swaransoft.com. "
    "We will acknowledge your grievance within 48 hours and resolve it within 30 days. Contact form submissions are retained for 24 months. "
    "Certain service providers may process data outside India (including in the USA and EU). " + "Padding sentence about nothing in particular. " * 12
)
DOC = PolicyDocument("https://swaransoft.com/privacy", "privacy_policy", POLICY, "crawl")


def res(id_, quote, doc=1, found=True):
    return PolicyReqResult(id=id_, found=found, quote=quote, document=doc)


def ext(*results):
    return PolicyExtraction(results=list(results))


# --- the catalogue ------------------------------------------------------------------------------------------------------------------

def test_the_catalogue_is_consistent():
    ids = [r.id for r in REQUIREMENTS]
    assert len(ids) == len(set(ids)) == len(BY_ID)
    assert {r.group for r in REQUIREMENTS} <= set(GROUPS)
    assert {r.basis for r in REQUIREMENTS} == {"required", "practice"}
    assert all(r.provisions and r.look_for and r.fix for r in REQUIREMENTS)


def test_every_provision_is_a_real_section_or_rule():
    act, rule = re.compile(r"^DPDP Act s\.(\d+)(\([a-z0-9]+\))*$"), re.compile(r"^DPDP Rules r\.(\d+)(\([a-z0-9]+\))*$")
    for r in REQUIREMENTS:
        for p in r.provisions:
            m = act.match(p) or rule.match(p)
            assert m, (r.id, p)
            n = int(m.group(1))
            assert (1 <= n <= 44) if p.startswith("DPDP Act") else (1 <= n <= 23), (r.id, p)


def test_the_mappings_that_a_third_party_report_got_wrong_are_right_here():
    prov = lambda i: " ".join(BY_ID[i].provisions)
    assert "s.6(9)" not in " ".join(p for r in REQUIREMENTS for p in r.provisions)   # s.6(9) is Consent Manager registration, not a cookie manager
    assert "s.5(3)" in prov("notice_language")                                        # the notice in English or an Eighth Schedule language
    assert "r.14(3)" in prov("grievance_timeline") and "s.13(2)" in prov("grievance_timeline")
    assert "s.8(9)" in prov("dpo_contact")                                            # business contact of the DPO or an answering person
    assert "r.14(1)(a)" in prov("rights_means") and "s.14" in prov("rights_nominate")
    assert "r.3(c)(i)" in prov("consent_withdrawal") and "s.6(4)" in prov("consent_withdrawal")
    assert "r.15" in prov("cross_border") and "s.16" in prov("cross_border")


def test_there_is_no_requirement_without_a_basis_in_the_act():
    assert not any("automated" in r.id or "automated" in r.title.lower() for r in REQUIREMENTS)   # a GDPR idea; the Act asks for no such disclosure
    assert not any(r.id == "dpo_identity" for r in REQUIREMENTS)


def test_duties_the_policy_is_not_required_to_state_are_marked_as_good_practice():
    assert {r.id for r in REQUIREMENTS if r.basis == "practice"} >= {"retention", "security", "breach_response", "cross_border", "data_sharing", "cookies_tracking"}
    assert {r.id for r in REQUIREMENTS if r.basis == "required"} >= {"notice_data_itemised", "notice_purposes", "rights_means", "grievance_timeline", "dpo_contact"}


# --- quotations ---------------------------------------------------------------------------------------------------------------------

def test_an_exact_quote_verifies_whatever_the_spacing_case_or_punctuation_style():
    assert verify_quote("We will acknowledge your grievance within 48 hours and resolve it within 30 days.", POLICY)
    assert verify_quote("we will  acknowledge your grievance\nwithin 48 hours and resolve it within 30 DAYS.", POLICY)
    assert verify_quote("Swaran Soft Support Solutions Pvt. Ltd. ('Swaran Soft') collects personal data you provide directly - your name", POLICY)   # curly quotes, em dash


def test_a_paraphrase_an_invention_or_a_stitched_quote_is_rejected():
    assert not verify_quote("Grievances are acknowledged in two days and resolved in a month.", POLICY)
    assert not verify_quote("We will acknowledge your grievance within 24 hours and resolve it within 30 days.", POLICY)
    assert not verify_quote("We will acknowledge your grievance within 48 hours ... Contact form submissions are retained for 24 months.", POLICY)
    assert not verify_quote("We retain data.", POLICY)             # too short to be evidence
    assert not verify_quote("x" * 600, "x" * 700)                   # too long


# --- the checks done in code --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,days", [
    ("within 48 hours", 2.0), ("within thirty (30) days", 30), ("resolve it within 30 days", 30), ("ninety days", 90), ("within 2 weeks", 14),
    ("acknowledge within 48 hours and resolve within 30 days", 30), ("within one month", 30), ("we will respond promptly", None),
])
def test_the_response_period_is_read_in_days(text, days):
    assert response_days(text) == days


def test_a_contact_needs_an_email_a_phone_number_or_a_form():
    assert has_contact("write to saurabhk@swaransoft.com")
    assert has_contact("call +91 124 456 7890")
    assert has_contact("use our contact form at https://x.test/contact")
    assert not has_contact("our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Pvt. Ltd.")


def _one(req_id, quote, text=POLICY, sector=None, child=False):
    doc = PolicyDocument("https://x.test/privacy", "privacy_policy", text, "crawl")
    out = analyse(ext(res(req_id, quote)), [doc], sector, child)
    return next(r for r in out["requirements"] if r["id"] == req_id)


def test_a_grievance_officer_with_an_email_beside_them_is_verified_and_without_one_is_partial():
    assert _one("grievance_contact", "Grievance Officer: Saurabh Kumar, Email: saurabhk@swaransoft.com.")["status"] == VERIFIED
    text = "Our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Private Limited. " + "Unrelated filler text. " * 40 + "Write to info@swaransoft.com for sales."
    row = _one("grievance_contact", "Our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Private Limited.", text)
    assert row["status"] == PARTIAL and "no email" in row["detail"]


def test_the_grievance_period_is_judged_against_the_ninety_day_limit():
    assert _one("grievance_timeline", "We will acknowledge your grievance within 48 hours and resolve it within 30 days.")["status"] == VERIFIED
    long = "Grievances are acknowledged promptly and resolved within 120 days of receipt by our team."
    row = _one("grievance_timeline", long, long + " " * 3 + "More text. " * 20)
    assert row["status"] == PARTIAL and "ninety" in row["detail"]
    vague = "We take every grievance seriously and will look into each complaint received."
    assert _one("grievance_timeline", vague, vague + " More text. " * 20)["status"] == PARTIAL


# --- analysing an extraction --------------------------------------------------------------------------------------------------------

def test_an_unquoted_requirement_is_not_found_not_a_violation():
    out = analyse(ext(), [DOC], None, False)
    row = next(r for r in out["requirements"] if r["id"] == "retention")
    assert row["status"] == NOT_FOUND and row["detail"] == "Not found in the policy text."
    assert "violation" not in row["detail"].lower()


def test_a_rejected_quote_is_counted_and_reported_as_not_found():
    out = analyse(ext(res("retention", "Contact form submissions are retained for forty months, not twenty-four.")), [DOC], None, False)
    row = next(r for r in out["requirements"] if r["id"] == "retention")
    assert row["status"] == NOT_FOUND and "rejected" in row["detail"] and row["quote"] == ""
    assert out["summary"]["quotes_rejected"] == 1


def test_a_quote_is_accepted_from_the_right_document_even_if_the_model_named_another():
    other = PolicyDocument("https://x.test/cookies", "cookie_policy", "We set cookies to remember your preferences. " * 8, "crawl")
    out = analyse(ext(res("retention", "Contact form submissions are retained for 24 months.", doc=1)), [other, DOC], None, False)
    row = next(r for r in out["requirements"] if r["id"] == "retention")
    assert row["status"] == VERIFIED and row["source_url"] == DOC.url


def test_a_requirement_the_model_found_but_which_has_no_quote_is_not_found():
    out = analyse(ext(res("retention", "", found=True)), [DOC], None, False)
    assert next(r for r in out["requirements"] if r["id"] == "retention")["status"] == NOT_FOUND


def test_the_childrens_requirement_applies_only_where_children_are_plausible():
    off = next(r for r in analyse(ext(), [DOC], "it", False)["requirements"] if r["id"] == "children_data")
    assert off["status"] == NOT_APPLICABLE and "children" in off["detail"]
    for sector in CHILD_SECTORS:
        assert next(r for r in analyse(ext(), [DOC], sector, False)["requirements"] if r["id"] == "children_data")["status"] == NOT_FOUND
    assert next(r for r in analyse(ext(), [DOC], "it", True)["requirements"] if r["id"] == "children_data")["status"] == NOT_FOUND


def test_the_completeness_figure_counts_partial_as_half_and_leaves_out_what_does_not_apply():
    out = analyse(ext(res("retention", "Contact form submissions are retained for 24 months."),
                      res("grievance_contact", "Our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Private Limited.")),
                  [PolicyDocument("https://x.test/p", "privacy_policy", "Contact form submissions are retained for 24 months. " + "Filler text. " * 50 + "Our Grievance Officer is Saurabh Kumar of Swaran Soft Support Solutions Private Limited. " + "Filler text. " * 50, "crawl")],
                  "it", False)
    s = out["summary"]
    assert s["applicable"] == len(REQUIREMENTS) - 1 and s["not_applicable"] == 1
    assert s["verified"] == 1 and s["partial"] == 1 and s["not_found"] == s["applicable"] - 2
    assert s["completeness_percent"] == round(1.5 / s["applicable"] * 100, 1)


def test_every_row_carries_its_legal_basis_from_the_catalogue_not_from_the_model():
    out = analyse(ext(res("retention", "Contact form submissions are retained for 24 months.")), [DOC], None, False)
    for row in out["requirements"]:
        assert row["provisions"] == list(BY_ID[row["id"]].provisions) and row["fix"] == BY_ID[row["id"]].fix


# --- documents ----------------------------------------------------------------------------------------------------------------------

def pol(url, ptype, text=None):
    return SimpleNamespace(url=url, policy_type=ptype, extracted_text_ref=text)


async def _no_fetch(url):
    return None


def test_privacy_and_cookie_policies_are_read_terms_are_not_and_one_page_is_read_once():
    cookies = "We set cookies to remember your preferences, to count visits and to measure campaigns, and you can change them. " * 4
    policies = [pol("https://www.x.test/privacy", "privacy_policy", POLICY), pol("https://x.test/privacy", "privacy_policy", POLICY),
                pol("https://x.test/cookies", "cookie_policy", cookies), pol("https://x.test/terms", "terms", POLICY)]
    docs, notes = asyncio.run(collect_documents(policies, _no_fetch))
    assert [d.policy_type for d in docs] == ["privacy_policy", "cookie_policy"] and notes == []


def test_two_policy_urls_with_identical_text_are_read_once_and_say_so():
    policies = [pol("https://x.test/privacy", "privacy_policy", POLICY), pol("https://x.test/cookie-policy", "cookie_policy", "  " + POLICY.upper().lower() + " ")]
    docs, notes = asyncio.run(collect_documents(policies, _no_fetch))
    assert len(docs) == 1 and docs[0].policy_type == "privacy_policy"
    assert notes == ["https://x.test/cookie-policy has the same text as https://x.test/privacy, so it was read once."]


def test_a_policy_with_no_captured_text_is_fetched_and_one_that_cannot_be_read_is_reported():
    async def fetch(url):
        return POLICY if url.endswith("/privacy") else None

    docs, notes = asyncio.run(collect_documents([pol("https://x.test/privacy", "privacy_policy", None), pol("https://x.test/cookies", "cookie_policy", "short")], fetch))
    assert len(docs) == 1 and docs[0].source == "fetch"
    assert len(notes) == 1 and "cookies" in notes[0] and "could not be read" in notes[0]


def test_a_very_long_policy_is_cut_to_the_budget_and_says_so():
    docs, _ = asyncio.run(collect_documents([pol("https://x.test/privacy", "privacy_policy", "word " * 20000)], _no_fetch))
    assert len(docs[0].text) == pa.MAX_DOC_CHARS and docs[0].truncated


# --- the whole step, fail-soft ------------------------------------------------------------------------------------------------------

class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))

    async def get(self, model, key):
        return SimpleNamespace(url="https://x.test/")


def _setup(monkeypatch, rows, evidence=None):
    monkeypatch.setattr(pa, "async_session_factory", lambda: _Db(rows))

    async def ev(scan_id, include_cookie_detail=False):
        return evidence or {"pages": [], "forms": [], "cookies": [], "trackers": [], "policies": [], "consent_signals": [], "third_party_services": []}

    monkeypatch.setattr(pa.scan_repository, "get_scan_evidence_summary_concurrent", ev)


def test_no_policy_page_gives_an_unavailable_result_with_the_reason(monkeypatch):
    _setup(monkeypatch, [])
    out = asyncio.run(pa.run(uuid.uuid4()))
    assert out["status"] == "unavailable" and "No privacy or cookie policy" in out["reason"] and out["requirements"] == [] and out["limitations"]


def test_unreadable_policies_give_an_unavailable_result(monkeypatch):
    _setup(monkeypatch, [pol("https://x.test/privacy", "privacy_policy", None)])
    out = asyncio.run(pa.run(uuid.uuid4(), fetcher=_no_fetch))
    assert out["status"] == "unavailable" and "could not be read" in out["reason"] and out["notes"]


def test_a_model_failure_never_raises_and_says_so(monkeypatch):
    _setup(monkeypatch, [pol("https://x.test/privacy", "privacy_policy", POLICY)])

    async def broken(system, user):
        raise TimeoutError("model unreachable")

    out = asyncio.run(pa.run(uuid.uuid4(), extractor=broken))
    assert out["status"] == "unavailable" and "could not be reached" in out["reason"] and out["documents"]


def test_a_successful_run_returns_the_requirements_the_documents_the_model_and_the_limits(monkeypatch):
    _setup(monkeypatch, [pol("https://x.test/privacy", "privacy_policy", POLICY)])
    seen = {}

    async def extractor(system, user):
        seen["system"], seen["user"] = system, user
        return ext(res("retention", "Contact form submissions are retained for 24 months.")), {"provider": "claude", "model": "m", "tokens_in": 10, "tokens_out": 5}

    out = asyncio.run(pa.run(uuid.uuid4(), extractor=extractor))
    assert out["status"] == "analysed" and out["model"]["provider"] == "claude"
    assert next(r for r in out["requirements"] if r["id"] == "retention")["status"] == VERIFIED
    assert out["documents"][0]["url"] == "https://x.test/privacy" and out["limitations"]
    assert "Document 1" in seen["user"] and "retention" in seen["user"] and "EXACTLY" in seen["system"]
    assert "children_data" not in seen["user"]       # not assessed for this site, so not asked about


def test_the_children_requirement_is_asked_about_for_an_education_site(monkeypatch):
    ev = {"pages": [{"url": "https://x.test/admissions", "title": "School admissions"}, {"url": "https://x.test/students", "title": "Students"}],
          "forms": [{"fields": [{"name": "student name"}, {"name": "parent email"}]}], "cookies": [], "trackers": [], "policies": [],
          "consent_signals": [], "third_party_services": []}
    _setup(monkeypatch, [pol("https://x.test/privacy", "privacy_policy", POLICY)], ev)
    seen = {}

    async def extractor(system, user):
        seen["user"] = user
        return ext(), {}

    out = asyncio.run(pa.run(uuid.uuid4(), extractor=extractor))
    assert "children_data" in seen["user"] and out["sector"] == "education"


def test_the_analysis_never_touches_the_score():
    from app.scoring import dpdp_score

    src = open(pa.__file__, encoding="utf-8").read()
    assert "dpdp_score" not in src.replace("never changes the DPDP Compliance Score", "")
    assert dpdp_score.METHODOLOGY_VERSION    # unchanged by this feature: the score is computed from the scan alone


# --- the crawler keeps the whole policy, once ---------------------------------------------------------------------------------------

def _link(href, text="Privacy Policy"):
    return ParsedLink(href=href, text=text)


def test_a_policy_keeps_its_full_text_and_is_listed_once_across_www_and_apex():
    long = "Privacy text. " * 3000                          # 42,000 characters: well past the old 2,000
    recs = detect_policies(
        [_link("https://www.x.test/privacy"), _link("https://x.test/privacy"), _link("https://x.test/cookie-policy", "Cookie Policy")],
        {"https://x.test/privacy": long}, "x.test",
    )
    assert [r.policy_type for r in recs] == ["privacy_policy", "cookie_policy"]    # one privacy policy, not one per spelling
    assert len(recs[0].extracted_text_ref) == len(long) > 2000                       # found via the other spelling, kept in full
    assert recs[1].extracted_text_ref is None


def test_policy_text_is_bounded():
    recs = detect_policies([_link("https://x.test/privacy")], {"https://x.test/privacy": "a" * (POLICY_TEXT_LIMIT + 5000)}, "x.test")
    assert len(recs[0].extracted_text_ref) == POLICY_TEXT_LIMIT


# --- the endpoint --------------------------------------------------------------------------------------------------------------------

client = TestClient(app)


def _headers():
    token = jwt.encode({"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated",
                        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _stage(result, status="completed", minutes=0):
    t = datetime.now(UTC) + timedelta(minutes=minutes)
    return SimpleNamespace(stage="policy_analysis", status=status, started_at=t, completed_at=t, stage_metadata={"result": result} if result else {})


def _route(monkeypatch, stages, scan=True):
    from app.db.session import get_db

    async def fake_db():
        yield None

    async def get_scan(db, scan_id, org_id):
        return object() if scan else None

    async def list_stages(db, scan_id, org_id):
        return stages

    monkeypatch.setattr(consent_scans.scan_repository, "get_scan", get_scan)
    monkeypatch.setattr(consent_scans.stage_repository, "list_stages", list_stages)
    app.dependency_overrides[get_db] = fake_db
    return get_db


def test_the_endpoint_returns_not_run_when_the_step_never_ran(monkeypatch):
    key = _route(monkeypatch, [])
    try:
        r = client.get(f"/api/v1/consent/scans/{uuid.uuid4()}/policy-analysis", headers=_headers())
        assert r.status_code == 200 and r.json()["status"] == "not_run" and r.json()["requirements"] == []
    finally:
        app.dependency_overrides.pop(key, None)


def test_the_endpoint_returns_the_latest_completed_result(monkeypatch):
    old = pa.unavailable("old")
    new = {**pa.unavailable("x"), "status": "analysed", "reason": None, "sector": "it", "summary": {
        "applicable": 19, "verified": 10, "partial": 1, "not_found": 8, "not_applicable": 1, "quotes_rejected": 0, "completeness_percent": 55.3},
        "requirements": [{"id": "retention", "title": "t", "group": "Transparency", "basis": "practice", "provisions": ["DPDP Act s.8(7)"], "status": "verified",
                          "quote": "q" * 30, "source_url": "https://x.test/p", "detail": "d", "fix": "f"}]}
    key = _route(monkeypatch, [_stage(old, minutes=-5), _stage(new), _stage(None, status="running", minutes=1)])
    try:
        r = client.get(f"/api/v1/consent/scans/{uuid.uuid4()}/policy-analysis", headers=_headers())
        body = r.json()
        assert r.status_code == 200 and body["status"] == "analysed" and body["summary"]["completeness_percent"] == 55.3
        assert body["requirements"][0]["provisions"] == ["DPDP Act s.8(7)"]
    finally:
        app.dependency_overrides.pop(key, None)


def test_the_endpoint_404s_for_a_scan_that_is_not_the_callers_and_refuses_no_token(monkeypatch):
    key = _route(monkeypatch, [], scan=False)
    try:
        assert client.get(f"/api/v1/consent/scans/{uuid.uuid4()}/policy-analysis", headers=_headers()).status_code == 404
        assert client.get(f"/api/v1/consent/scans/{uuid.uuid4()}/policy-analysis").status_code == 401
    finally:
        app.dependency_overrides.pop(key, None)
