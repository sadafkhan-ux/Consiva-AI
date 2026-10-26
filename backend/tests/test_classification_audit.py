"""Vendor, cookie and vendor-mention matching after the 2026-10-26 audit: whole labels, whole names, domain-aware, and the same answer for every site."""

from types import SimpleNamespace

import pytest

from app.agents.consent_agent.nodes.validate_output import _names_vendor, _unsupported_vendor_claims
from app.lookup.matcher import MIN_PREFIX_LENGTH, LookupEntry, match_cookie_lookup
from app.rules.consent_rules import classify_cookie, classify_tracker
from app.rules.tracker_catalog import match_vendor
from app.scanner.schemas import CookieRecord, TrackerRecord


def vendor_of_url(url, resource_type=None):
    t = classify_tracker(TrackerRecord(local_id="t", script_src=url, resource_type=resource_type))
    return t.vendor, t.category


# ── a request is matched on its host and path, never on its query string ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,vendor", [
    ("https://www.google-analytics.com/g/collect?v=2", "Google Analytics"),
    ("https://static.criteo.net/js/ld/publishertag.js", "Criteo"),
    ("https://www.clarity.ms/tag/abc", "Microsoft Clarity"),
    ("https://www.facebook.com/tr/?id=1", "Meta Pixel"),
    ("https://www.facebook.com/tr?id=1", "Meta Pixel"),
    ("https://www.google.co.in/ads/ga-audiences?v=1", "Google Ads"),
    ("https://js.stripe.com/v3", "Stripe"),
    ("https://fonts.gstatic.com/s/inter.woff2", "Google Fonts"),
])
def test_a_real_vendor_request_is_matched_whatever_site_it_was_seen_on(url, vendor):
    assert vendor_of_url(url)[0] == vendor


@pytest.mark.parametrize("url", [
    "https://cdn.example.net/a.js?ref=vimeo.com",         # the vendor's name inside a query string
    "https://cheap.io/x.js",                             # "heap.io" is not a label of cheap.io
    "https://clarity.msu.edu/x.js",                      # "clarity.ms" is not a label of clarity.msu.edu
    "https://www.facebook.com/trending",                 # "facebook.com/tr" is not the path /trending
    "https://shop.example/img/photo.png?utm=hotjar.com",
])
def test_a_name_that_only_looks_like_a_vendor_is_not_matched(url):
    assert vendor_of_url(url) == (None, None)


# ── cookie names: whole names for fixed cookies, prefixes only where the cookie really is a family ───────────────────────────

def cookie_vendor(name, domain="shop.example", entries=()):
    c = classify_cookie(CookieRecord(local_id="c", name=name, domain=domain), list(entries))
    return c.vendor, c.category, c.source


def test_a_fixed_cookie_name_is_not_a_prefix_that_captures_other_cookies():
    assert cookie_vendor("IDE")[:2] == ("Google Ads", "marketing")
    assert cookie_vendor("IDENTITY") == (None, None, None)
    assert cookie_vendor("IDEA") == (None, None, None)
    assert cookie_vendor("li_sugr")[0] == "LinkedIn Insight Tag" and cookie_vendor("li_theme") == (None, None, None)
    assert cookie_vendor("ph_phc_abc123_posthog")[0] == "PostHog" and cookie_vendor("ph_settings") == (None, None, None)


def test_cookie_names_match_without_regard_to_case():
    assert match_vendor(cookie_name="_GA").vendor == "Google Analytics"
    assert match_vendor(cookie_name="AMP_abc").vendor == "Amplitude"


def test_the_cloudflare_rate_limit_cookie_is_classified_by_the_vendor_list_without_the_lookup_table():
    assert cookie_vendor("_cfuvid", "calendly.com") == ("Cloudflare", "functional", "rule")
    assert cookie_vendor("__cf_bm", "other.example")[:2] == ("Cloudflare", "functional")


def test_a_cookie_set_on_a_vendors_own_domain_is_matched_by_the_domain():
    assert cookie_vendor("anything", "forms.hubspot.com")[:2] == ("HubSpot", "marketing")
    assert cookie_vendor("anything", "cheap.io") == (None, None, None)


# ── the lookup table: no one-letter wildcards, no generic names without their domain ─────────────────────────────────────────

ENTRIES = [
    LookupEntry(name_pattern="_s", is_prefix_pattern=True, category="analytics", vendor="Marfeel"),
    LookupEntry(name_pattern="CT", is_prefix_pattern=True, category="marketing", vendor="Adform"),
    LookupEntry(name_pattern="_ga_", is_prefix_pattern=True, category="analytics", vendor="Google Analytics"),
    LookupEntry(name_pattern="uid", is_prefix_pattern=False, category="marketing", vendor="Adform", domain_pattern="adform.net"),
    LookupEntry(name_pattern="token", is_prefix_pattern=False, category="marketing", vendor="Adform"),
    LookupEntry(name_pattern="_fbp", is_prefix_pattern=False, category="marketing", vendor="Facebook", domain_pattern="facebook.com (3rd party)"),
]


@pytest.mark.parametrize("name", ["_session_id", "_stripe_mid", "CTX"])
def test_a_wildcard_shorter_than_the_minimum_never_matches(name):
    assert MIN_PREFIX_LENGTH == 4
    assert match_cookie_lookup(name, ENTRIES, "shop.example") is None


def test_a_proper_wildcard_still_matches():
    assert match_cookie_lookup("_ga_ABC123", ENTRIES, "shop.example").vendor == "Google Analytics"


def test_a_generic_name_counts_only_on_the_entrys_own_domain():
    assert match_cookie_lookup("uid", ENTRIES, "shop.example") is None
    assert match_cookie_lookup("uid", ENTRIES, None) is None
    assert match_cookie_lookup("uid", ENTRIES, "sync.adform.net").vendor == "Adform"
    assert match_cookie_lookup("token", ENTRIES, "adform.net") is None        # a generic name with no domain on its entry is not evidence of anything


def test_a_specific_name_is_matched_wherever_the_site_sets_it():
    """`_fbp` is set by the Meta Pixel on the VISITED site's own domain, so its entry's domain must not be required."""
    assert match_cookie_lookup("_fbp", ENTRIES, "shop.example").vendor == "Facebook"


# ── a finding names a vendor only when it names it ──────────────────────────────────────────────────────────────────────────────

def finding(text):
    return SimpleNamespace(finding=text, recommendation="")


def test_an_ordinary_word_is_not_a_vendor_mention():
    assert not _names_vendor("analytics fired before consent for every visitor segment", "Segment")
    assert not _names_vendor("traffic segments were measured", "Segment")
    assert _names_vendor("Segment fired before consent", "Segment")
    assert not _names_vendor("this cookiebot-free site", "Cookiebot")
    assert _names_vendor("google analytics fired", "Google Analytics")


def test_a_finding_about_visitor_segments_is_not_judged_to_blame_a_vendor():
    text = "Analytics fired before consent for every visitor segment."
    assert _unsupported_vendor_claims(finding(text), [], []) == []


def test_a_real_unsupported_vendor_claim_is_still_caught():
    claims = _unsupported_vendor_claims(finding("Google Analytics fired before consent."), [], [])
    assert claims == ["Google Analytics (named as firing before consent)"]
    assert _unsupported_vendor_claims(finding("Google Analytics fired before consent."), ["Google Analytics"], []) == []
