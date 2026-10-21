"""A page that only frames another site must not be reported as if it were that site.

smartians.ai is a <frameset> with one <frame> onto corp.gignaati.com. The scan saw the wrapper only, and reported two
HIGH findings -- "no consent banner" and "no privacy policy" -- as facts about the site visitors actually see. The
wrapper is now detected, the absence claims are marked low-confidence with an explanation, and R-012 says to scan the
framed site directly.
"""

from app.rules.consent_rules import evaluate_consent_rules
from app.scanner.crawler import _frameset_sources
from tests.test_scanner_coverage import ROOT, _FakeContext, _crawl

FRAMESET = '<html><head><title>x</title></head><frameset rows="100%"><frame src="https://corp.example.org/" name="main"></frameset></html>'


def test_a_frameset_page_reports_where_its_frames_point():
    assert _frameset_sources(FRAMESET, "https://smartians.test/") == ["https://corp.example.org/"]
    assert _frameset_sources('<frameset><frame src="/inner"></frameset>', "https://a.test/x") == ["https://a.test/inner"]


def test_ordinary_pages_and_iframe_widgets_are_not_wrappers():
    assert _frameset_sources('<html><body><iframe src="https://widget.test/"></iframe></body></html>', "https://a.test/") == []
    assert _frameset_sources("<html><body>hello</body></html>", "https://a.test/") == []
    assert _frameset_sources("", "https://a.test/") == []


async def test_the_crawl_records_a_framed_other_site_and_ignores_a_frame_on_the_same_site():
    other = _FakeContext({f"{ROOT}/": FRAMESET.replace("https://corp.example.org/", "https://corp.other.test/")})
    same = _FakeContext({f"{ROOT}/": FRAMESET.replace("https://corp.example.org/", f"{ROOT}/inner")})

    *_a, d_other = await _crawl(other)
    *_b, d_same = await _crawl(same)

    assert d_other["framed_sites"] == ["https://corp.other.test/"]
    assert d_same["framed_sites"] == []


def _evidence(framed=None):
    signal_evidence = {"accept_interaction": "cmp_not_found", "reject_interaction": "cmp_not_found"}
    if framed:
        signal_evidence["framed_sites"] = framed
    return {
        "cookies": [], "forms": [], "policies": [],
        "trackers": [{"id": "t1", "script_src": "https://calendly.com/x", "vendor": None, "category": "marketing", "source": "rule",
                      "consent_states": ["pre_consent"], "page_url": "https://smartians.ai/"}],
        "consent_signals": [{"mechanism_type": "none", "cmp_vendor": None, "has_reject_all": None, "has_granular_choices": None,
                             "evidence": signal_evidence}],
    }


def _by_rule(findings):
    return {f.rule_id: f for f in findings}


def test_absence_findings_on_a_wrapper_are_low_confidence_and_say_why_and_r012_is_added():
    found = _by_rule(evaluate_consent_rules(_evidence(["https://corp.gignaati.com/"])))

    for rule in ("R-002", "R-006"):
        assert found[rule].confidence == "low", "a wrapper cannot show that the site has no banner or policy"
        assert found[rule].risk_level == "high", "reported, not suppressed"
        assert "only displays another site (https://corp.gignaati.com/)" in found[rule].summary and "R-012" in found[rule].summary
    assert found["R-012"].risk_level == "medium"
    assert "Scan https://corp.gignaati.com/ directly" in found["R-012"].summary


def test_an_ordinary_site_is_unchanged():
    found = _by_rule(evaluate_consent_rules(_evidence()))

    assert found["R-002"].confidence == "high" and found["R-006"].confidence == "high"
    assert "R-012" not in found and "wrapper" not in found["R-002"].summary
