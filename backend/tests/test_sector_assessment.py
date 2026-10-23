"""Sector detection and the provisions that depend on it, for the ten sectors the console offers (plus legal services and general).

The sector is informational: it must never change a score, and it must not rest on the host name alone."""

import dataclasses

import pytest

from app.scoring import dpdp_score
from app.scoring.dpdp_score import Applicability
from app.scoring.sector import SECTORS, assess_sector


def ev(title="", paths=(), fields=(), services=(), trackers=(), cookies=(), policies=(), consent=()):
    pages = [{"url": f"https://example.test{p}", "title": title} for p in (paths or ("/",))]
    forms = [{"id": "f1", "selector": "form", "purpose_guess": None, "fields": [{"name": n, "required": False, "field_type": "text"} for n in fields]}] if fields else []
    return {"pages": pages, "forms": forms, "cookies": [{"name": c} for c in cookies], "trackers": list(trackers),
            "policies": [{"policy_type": p, "url": f"https://example.test/{p}"} for p in policies], "consent_signals": list(consent),
            "third_party_services": [{"service_name": s, "category": None, "domains": []} for s in services]}


CASES = {
    "healthcare": ev("City Hospital | Book a doctor appointment", ["/patients", "/diagnostics"], ["patient name", "symptoms"]),
    "education": ev("Greenfield School admissions", ["/admissions", "/students"], ["student name", "parent email"]),
    "bfsi": ev("Home loans and wealth management | Net banking", ["/loans", "/banking"], ["pan", "account number"]),
    "insurance": ev("Health insurance and motor insurance | Claim settlement", ["/insurance", "/claims"], ["policy number", "nominee name"]),
    "real_estate": ev("Luxury apartments and villas | RERA registered", ["/properties", "/floor-plans"], ["name", "budget", "site visit date"]),
    "retail": ev("Shop sarees online | Free shipping", ["/cart", "/checkout"], ["coupon"], ["Shopify"]),
    "manufacturing": ev("Industrial machinery manufacturer | ISO 9001", ["/manufacturing", "/spare-parts"], ["company", "quantity"]),
    "hospitality": ev("Beachfront resort and hotel | Book rooms", ["/rooms", "/restaurant"], ["check in", "check out", "guests", "children"]),
    "it": ev("IT services and application development | Managed services outsourcing", ["/it-services", "/outsourcing"], ["email"]),
    "saas": ev("Pricing plans | Start free trial | Book a demo", ["/pricing", "/integrations"], ["work email"]),
    "technology": ev("Artificial intelligence and cloud engineering | Machine learning", ["/cloud", "/devops"], ["email"]),
    "government_vendor": ev("Tender and e-governance solutions for government clients", ["/tenders", "/smart-city"], ["company", "tender number"]),
    "legal": ev("Sharma & Associates | Law firm", ["/advocates", "/litigation"], ["case details"]),
}

TEN = ["healthcare", "education", "bfsi", "insurance", "real_estate", "retail", "manufacturing", "hospitality", "it", "saas", "technology", "government_vendor", "legal"]


def test_every_requested_sector_exists_and_general_is_the_fallback():
    assert set(TEN) | {"general"} == set(SECTORS)
    assert len(SECTORS) == 14
    assert SECTORS["bfsi"].startswith("BFSI") and SECTORS["government_vendor"] == "Government vendor" and SECTORS["real_estate"] == "Real estate"


@pytest.mark.parametrize("sector", list(CASES))
def test_each_sector_is_recognised_from_what_the_scan_stored(sector):
    result = assess_sector(CASES[sector])
    assert result.sector == sector, result.sector_scores
    assert result.label == SECTORS[sector]
    assert result.confidence in ("medium", "high")
    assert result.signals and all(s.sector == sector for s in result.signals)
    assert result.notes and result.limitations
    assert result.declared is False and result.detected_sector == sector


@pytest.mark.parametrize("sector", TEN)
def test_each_sector_has_its_own_notes_citing_the_act_or_rules(sector):
    notes = assess_sector(CASES[sector]).notes
    assert notes and all(n.provisions and n.text and n.action for n in notes)
    assert not any(n.id == "general" for n in notes)
    assert all(any(p.startswith(("DPDP Act", "DPDP Rules")) for p in n.provisions) for n in notes)


def test_insurance_and_bfsi_are_told_apart():
    assert assess_sector(CASES["insurance"]).sector == "insurance"
    assert assess_sector(CASES["bfsi"]).sector == "bfsi"


def test_the_notes_cite_provisions_that_exist_in_the_act_and_rules():
    cited = {
        "healthcare": ("r.12", "Fourth Schedule", "s.9"),
        "education": ("Fourth Schedule Part A item 3", "r.10"),
        "retail": ("Third Schedule", "r.8", "s.7(a)"),
        "bfsi": ("s.8(7)", "illustration (II)", "s.17(1)(f)"),
        "insurance": ("s.8(7)", "s.10(1)(a)", "r.10"),
        "real_estate": ("s.7(a), illustration (II)",),
        "government_vendor": ("s.7(b)", "s.17(2)(a)", "s.8(2)"),
        "it": ("s.8(2)", "s.2(t)"), "saas": ("s.8(2)", "s.2(t)"), "technology": ("s.8(2)", "s.2(t)"),
        "manufacturing": ("s.2(t)", "s.8(2)"),
        "hospitality": ("s.9(1)", "s.7(a)"),
    }
    for sector, needles in cited.items():
        text = " ".join(" ".join(n.provisions) for n in assess_sector(CASES[sector]).notes)
        for needle in needles:
            assert needle in text, (sector, needle)


# --- evidence, not the domain name ---------------------------------------------------------------------------------------------

def test_a_host_name_alone_never_names_a_sector():
    result = assess_sector(ev("Welcome"), scan_url="https://www.apollohospitals.com/")
    assert result.sector == "general" and result.confidence == "low"
    assert "nothing else" in result.summary and "host" in result.summary


def test_the_host_name_still_counts_when_something_on_the_site_agrees_with_it():
    result = assess_sector(ev("Book an appointment with our doctors", ["/patients"]), scan_url="https://www.apollohospitals.com/")
    assert result.sector == "healthcare"
    assert {"host", "title"} <= {s.source for s in result.signals}


def test_a_run_together_host_name_is_read_but_short_terms_never_match_inside_it():
    assert any(s.source == "host" for s in assess_sector(CASES["healthcare"], scan_url="https://www.apollohospitals.com/").signals)
    assert assess_sector(ev("Welcome"), scan_url="https://www.mainstreamedia.com/").sector == "general"


def test_a_payment_gateway_is_not_evidence_of_a_sector():
    result = assess_sector(ev("Welcome", ["/"], ["email"], services=["Razorpay", "Stripe"]))
    assert result.sector == "general"
    assert result.sector_scores["bfsi"] == 0


def test_a_shop_platform_or_cookie_is_evidence_of_retail():
    result = assess_sector(ev("Welcome to our store", ["/products"], services=["Shopify"], cookies=["_shopify_y"]))
    assert result.sector == "retail" and {"service", "cookie"} <= {s.source for s in result.signals}


def test_a_booking_engine_is_evidence_of_hospitality():
    result = assess_sector(ev("Stay with us", ["/rooms"], ["check in", "check out"], services=["SiteMinder"]))
    assert result.sector == "hospitality"


def test_a_single_weak_word_does_not_name_a_sector():
    assert assess_sector(ev("Our health and wellness blog")).sector == "general"


def test_a_repeated_title_is_counted_once_not_once_per_page():
    one = assess_sector(ev("Hospital", ["/"]))
    five = assess_sector(ev("Hospital", ["/", "/a", "/b", "/c", "/d"]))
    assert one.sector_scores["healthcare"] == five.sector_scores["healthcare"]


def test_two_close_sectors_are_not_forced_into_one():
    assert assess_sector(ev("Hospital and bank", ["/"], [])).sector == "general"


def test_a_site_with_no_sector_signals_is_general_with_low_confidence():
    result = assess_sector(ev("Welcome", ["/", "/about"], ["email"]))
    assert (result.sector, result.confidence, result.signals) == ("general", "low", [])
    assert result.notes[0].id == "general"


# --- what the scan saw, reported against each sector's provisions --------------------------------------------------------------

def test_the_general_note_reports_the_notice_and_banner_the_scan_found():
    obs = " ".join(assess_sector(ev("Welcome", policies=["privacy_policy"], consent=[{"mechanism_type": "cmp", "cmp_vendor": "Consiva Consent SDK", "has_reject_all": True}])).notes[0].observed)
    assert "privacy policy" in obs and "Consiva Consent SDK" in obs and "reject-all present" in obs


def test_children_signals_are_reported_with_the_field_names():
    result = assess_sector(CASES["education"])
    obs = " ".join(" ".join(n.observed) for n in result.notes)
    assert "parent email" in obs and "student name" in obs
    assert any(d.flag == "children" and d.requirement_id == "DPDP-X10" for d in result.declarations)


def test_hospitality_reports_booking_identity_and_children_fields():
    ev_ = ev("Beachfront resort and hotel | Book rooms", ["/rooms", "/restaurant"], ["check in", "check out", "guests", "children", "passport number"])
    obs = " ".join(" ".join(n.observed) for n in assess_sector(ev_).notes)
    assert "check in" in obs and "passport number" in obs and "children" in obs


def test_real_estate_reports_enquiry_and_identity_fields():
    ev_ = ev("Luxury apartments and villas | RERA registered", ["/properties", "/floor-plans"], ["name", "budget", "pan"])
    obs = " ".join(" ".join(n.observed) for n in assess_sector(ev_).notes)
    assert "budget" in obs and "pan" in obs


def test_declarations_follow_the_sector_and_show_what_was_declared():
    flags = lambda s: {d.flag for d in assess_sector(CASES[s]).declarations}
    assert flags("retail") == {"erasure_class", "significant"}
    assert flags("healthcare") == {"children", "significant"}
    assert "significant" in flags("bfsi") and "significant" in flags("insurance") and "significant" in flags("government_vendor")
    assert "children" in flags("insurance") and "children" in flags("hospitality")
    declared = assess_sector(CASES["retail"], applicability=Applicability(erasure_class=True))
    pick = lambda r: next(d for d in r.declarations if d.flag == "erasure_class").declared
    assert pick(declared) is True and pick(assess_sector(CASES["retail"])) is None


def test_trackers_are_summarised_with_how_many_fired_before_consent():
    t = [{"category": "analytics", "vendor": "Google Analytics", "consent_states": ["pre_consent"]},
         {"category": "marketing", "vendor": "Meta Pixel", "consent_states": ["post_accept"]}]
    ev_ = ev("Greenfield School admissions", ["/students"], [], trackers=t)
    obs = " ".join(" ".join(n.observed) for n in assess_sector(ev_).notes)
    assert "2 analytics or marketing request(s)" in obs and "1 of them before any consent choice" in obs


# --- business context: a declared sector --------------------------------------------------------------------------------------

def test_a_declared_sector_is_used_and_the_scans_own_reading_is_shown_beside_it():
    result = assess_sector(CASES["it"], declared_sector="government_vendor")
    assert result.sector == "government_vendor" and result.declared and result.confidence == "declared"
    assert result.detected_sector == "it" and result.agrees_with_scan is False
    assert "Declared by the organisation" in result.summary and "it services" in result.summary.lower()
    assert result.notes[0].id == "government-vendor-not-the-state"
    assert "IT services" in result.also_possible


def test_a_declared_sector_that_matches_the_scan_says_so():
    result = assess_sector(CASES["healthcare"], declared_sector="healthcare")
    assert result.agrees_with_scan is True and "same" in result.summary


def test_declaring_a_sector_works_when_the_scan_found_too_little_to_name_one():
    result = assess_sector(ev("Welcome"), declared_sector="manufacturing")
    assert result.sector == "manufacturing" and result.detected_sector == "general" and "too little evidence" in result.summary


def test_an_unknown_or_general_declaration_is_ignored():
    for bad in ("nonsense", "general", ""):
        result = assess_sector(CASES["healthcare"], declared_sector=bad)
        assert result.sector == "healthcare" and result.declared is False


# --- the score is never affected ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", TEN)
def test_the_sector_never_changes_the_score(key, monkeypatch):
    evidence = CASES[key]
    with_sector = dpdp_score.assess(evidence)
    assert with_sector.sector.sector == key
    declared = dpdp_score.assess(evidence, business_sector="legal")
    assert declared.sector.sector == "legal"
    monkeypatch.setattr(dpdp_score.sector_analysis, "assess_sector", lambda *a, **k: None)
    without = dpdp_score.assess(evidence)
    assert without.sector is None
    for field in ("readiness", "in_force", "status_counts", "ledger"):
        a, b, c = (dataclasses.asdict(x)[field] for x in (with_sector, without, declared))
        assert a == b == c, field
    assert [(r.id, r.status) for r in with_sector.requirements] == [(r.id, r.status) for r in without.requirements]


def test_entity_specific_requirements_stay_excluded_whatever_the_sector():
    for key, req in (("retail", "DPDP-X8"), ("education", "DPDP-X10"), ("insurance", "DPDP-X10"), ("government_vendor", "DPDP-X13"), ("bfsi", "DPDP-X13")):
        for kwargs in ({}, {"business_sector": key}):
            status = {r.id: r.status for r in dpdp_score.assess(CASES[key], **kwargs).requirements}
            assert status[req] == "not_applicable", (key, req, kwargs)
