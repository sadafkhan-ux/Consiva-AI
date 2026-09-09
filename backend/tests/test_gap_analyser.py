"""Tests for the consent-gap analyser's deterministic layer.

No browser and no network: everything below feeds already-captured page data into the
pure detection/scoring functions. That is the point of the split -- the numbers quoted
to a prospect must be reproducible, so they must be testable without a live site.

Several tests exist specifically to pin the gotchas called out in the spec, each of
which was a real field failure.
"""

import time

import pytest

from app.gap_analyser import detect, scoring
from app.gap_analyser.analyser import _pick_page_text


# ---------------------------------------------------------------------------
# CMP detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("html", "srcs", "expected"),
    [
        ('<div id="onetrust-banner-sdk">', [], "OneTrust"),
        ("", ["https://consent.cookiebot.com/uc.js"], "Cookiebot"),
        ("", ["https://cdn-cookieyes.com/client_data/x/script.js"], "CookieYes"),
        # The two WordPress plugins are the most common CMPs on Indian SME sites.
        ('<div class="cli-modal">', [], "GDPR Cookie Consent (WP)"),
        ('<div id="moove_gdpr_cookie_info_bar">', [], "Moove GDPR (WP)"),
        ("<p>nothing here</p>", ["/js/app.js"], None),
    ],
)
def test_detect_cmp(html, srcs, expected):
    has_cmp, name = detect.detect_cmp(html, srcs)
    assert name == expected
    assert has_cmp is (expected is not None)


def test_detect_cmp_matches_script_src_not_just_html():
    """The vendor is frequently only visible in a <script src>, never in the markup."""
    has_cmp, name = detect.detect_cmp("<html><body>hi</body></html>",
                                      ["https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"])
    assert (has_cmp, name) == (True, "OneTrust")


# ---------------------------------------------------------------------------
# Banner without a recognised vendor
# ---------------------------------------------------------------------------
def test_banner_detected_from_selector_hint():
    assert detect.detect_cookie_banner('<div class="cookie-notice-container">', "") is True


def test_banner_detected_from_text_only():
    assert detect.detect_cookie_banner("<div>x</div>", "We use cookies to improve your visit.") is True


def test_no_banner_when_nothing_matches():
    assert detect.detect_cookie_banner("<div>welcome</div>", "Welcome to our site") is False


# ---------------------------------------------------------------------------
# Cookie classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "party", "expected"),
    [
        ("_fbp", "third", "advertising"),
        ("IDE", "third", "advertising"),
        ("_hjSessionUser_123", "first", "session_replay"),
        ("_clck", "first", "session_replay"),
        ("_ga", "first", "analytics"),
        ("__hstc", "first", "analytics"),
        # The rule that matters most: unrecognised THIRD-party cookies are trackers.
        # CLID / SRM_B / ANONCHK matched no name list and were a third of one real
        # site's trackers.
        ("CLID", "third", "third_party"),
        ("SRM_B", "third", "third_party"),
        ("ANONCHK", "third", "third_party"),
        ("PHPSESSID", "first", "functional"),
    ],
)
def test_classify_cookie(name, party, expected):
    assert detect.classify_cookie(name, party) == expected


def test_cookie_record_never_stores_value():
    """Cookie values are live tracking identifiers for real visitors and arguably
    personal data under DPDP -- they must not survive capture."""
    raw = {"name": "_ga", "value": "GA1.2.SECRET-VISITOR-ID", "domain": ".example.com",
           "expires": time.time() + 400 * 86400, "secure": True, "sameSite": "Lax", "httpOnly": False}
    record = detect.build_cookie_record(raw, "example.com")

    assert "value" not in record
    assert "SECRET-VISITOR-ID" not in str(record)
    assert record["party"] == "first"
    assert record["lifetime_days"] == pytest.approx(399, abs=2)


def test_cookie_party_uses_final_domain_after_redirect():
    """Gotcha 2: a site requested at one domain and served from another. Comparing
    against the requested host mislabels every first-party cookie as third-party."""
    raw = {"name": "session", "domain": ".ronaldindia.com", "expires": -1}
    # Served from ronaldindia.com after redirect from ronaldweboffset.com.
    assert detect.build_cookie_record(raw, "ronaldindia.com")["party"] == "first"
    assert detect.build_cookie_record(raw, "ronaldweboffset.com")["party"] == "third"


def test_session_cookie_has_null_lifetime():
    record = detect.build_cookie_record({"name": "x", "domain": "example.com", "expires": -1}, "example.com")
    assert record["session"] is True
    assert record["lifetime_days"] is None


def test_summarise_cookies_counts():
    now = time.time()
    cookies = [
        detect.build_cookie_record({"name": "_ga", "domain": ".example.com", "expires": now + 400 * 86400}, "example.com"),
        detect.build_cookie_record({"name": "IDE", "domain": ".doubleclick.net", "expires": now + 399 * 86400}, "example.com"),
        detect.build_cookie_record({"name": "PHPSESSID", "domain": "example.com", "expires": -1}, "example.com"),
    ]
    s = detect.summarise_cookies(cookies)
    assert s["cookies_total"] == 3
    assert s["tracking_cookie_count"] == 2          # functional one excluded
    assert s["third_party_domains"] == ["doubleclick.net"]
    assert s["long_lived_count"] == 2
    assert s["max_cookie_lifetime_days"] >= 398


# ---------------------------------------------------------------------------
# Form field classification
# ---------------------------------------------------------------------------
def test_email_address_overlap_does_not_report_postal_address():
    """"Email Address" matches both patterns. Without discarding `address` on an email
    hit, every contact form falsely reports collecting postal addresses."""
    assert detect.classify_field("Email Address") == {"email"}


def test_real_address_still_detected():
    assert "address" in detect.classify_field("Shipping Address / City / Pincode")


def test_message_only_form_is_not_a_finding():
    forms = [{"action": "", "field_descriptors": ["Your message"],
              "has_consent_checkbox": False, "has_privacy_link": False}]
    assert detect.analyse_forms(forms, "https://example.com")["has_data_form"] is False


def test_identifying_form_is_a_finding():
    forms = [{"action": "", "field_descriptors": ["Full Name", "Email", "Mobile"],
              "has_consent_checkbox": False, "has_privacy_link": False}]
    out = detect.analyse_forms(forms, "https://example.com")
    assert out["has_data_form"] is True
    assert out["form_field_types"] == ["email", "name", "phone"]


def test_external_form_action_is_recorded():
    forms = [{"action": "https://webto.salesforce.com/servlet/x", "field_descriptors": ["Email"],
              "has_consent_checkbox": False, "has_privacy_link": False}]
    out = detect.analyse_forms(forms, "https://example.com/contact")
    assert out["form_third_party_hosts"] == ["webto.salesforce.com"]


def test_relative_form_action_is_not_external():
    forms = [{"action": "/submit", "field_descriptors": ["Email"],
              "has_consent_checkbox": False, "has_privacy_link": False}]
    assert detect.analyse_forms(forms, "https://example.com/contact")["form_third_party_hosts"] == []


def test_consent_checkbox_requires_every_collecting_form():
    """One unprotected form is still a gap, so this is all() not any()."""
    forms = [
        {"action": "", "field_descriptors": ["Email"], "has_consent_checkbox": True, "has_privacy_link": False},
        {"action": "", "field_descriptors": ["Phone"], "has_consent_checkbox": False, "has_privacy_link": False},
    ]
    assert detect.analyse_forms(forms, "https://example.com")["form_consent_checkbox"] is False


# ---------------------------------------------------------------------------
# Tech stack
# ---------------------------------------------------------------------------
def test_ga4_id_is_case_sensitive():
    """A lowercase "g-" substring search matches almost every page on the web."""
    assert "Google Analytics 4" in detect.detect_tech("<p>G-ABCD1234</p>", [])["tech_analytics_tools"]
    assert "Google Analytics 4" not in detect.detect_tech("<p>going-somewhere</p>", [])["tech_analytics_tools"]


def test_tech_stack_detection():
    html = '<link href="/wp-content/themes/x/style.css">'
    srcs = ["https://checkout.razorpay.com/v1/checkout.js", "https://www.clarity.ms/tag/x"]
    tech = detect.detect_tech(html, srcs)
    assert tech["tech_cms"] == "WordPress"
    assert tech["tech_payment_gateway"] == "Razorpay"
    assert "Microsoft Clarity" in tech["tech_analytics_tools"]


# ---------------------------------------------------------------------------
# Page status -- parked is checked BEFORE blocked
# ---------------------------------------------------------------------------
def test_parked_domain_for_sale():
    assert detect.detect_page_status(
        title="example.in", body_text="This domain is for sale",
        http_status=200, final_domain="example.in") == "parked"


def test_title_equal_to_hostname_is_parked():
    assert detect.detect_page_status(
        title="elohi.in", body_text="something short",
        http_status=200, final_domain="elohi.in") == "parked"


def test_blocked_interstitial():
    assert detect.detect_page_status(
        title="Just a moment...", body_text="Checking your browser before accessing",
        http_status=403, final_domain="example.com") == "blocked"


def test_suspended_store_is_parked_not_blocked():
    """Checked parked-first on purpose: dead stores answer 402/503 and would otherwise
    be retried forever as if they were transient blocks."""
    assert detect.detect_page_status(
        title="Shop", body_text="This store is currently unavailable",
        http_status=402, final_domain="shop.com") == "parked"


def test_5xx_with_real_content_is_still_usable():
    """A 503 that renders real content is NOT blocked -- the page still names the shop."""
    body = "Acme Industrial Supplies " + ("real product content " * 40)
    assert detect.detect_page_status(
        title="Acme Industrial Supplies", body_text=body,
        http_status=503, final_domain="acme.com") == "ok"


# ---------------------------------------------------------------------------
# Page-text extraction (gotcha 4)
# ---------------------------------------------------------------------------
def test_tiny_main_container_falls_back_to_body():
    """A real WordPress site returned 207 chars out of 10,016 from <article>, and every
    extracted field came back null as a result."""
    main = "x" * 207
    body = "y" * 10016
    assert _pick_page_text(main, body) == body


def test_substantial_main_container_is_preferred():
    main = "x" * 8000
    body = "y" * 10000
    assert _pick_page_text(main, body) == main


# ---------------------------------------------------------------------------
# Scoring -- must be a pure function of the observations
# ---------------------------------------------------------------------------
def _score(**over):
    base = {
        "has_cmp": False, "has_cookie_banner": False, "has_privacy_policy": True,
        "cookies": [], "third_party_count": 0, "long_lived_count": 0,
        "has_data_form": False, "form_consent_checkbox": False,
    }
    base.update(over)
    return scoring.score_consent_gap(**base)


def test_site_with_cmp_scores_zero():
    assert _score(has_cmp=True, cookies=[{"category": "advertising"}] * 20,
                  third_party_count=15, long_lived_count=10) == 0


def test_score_is_deterministic():
    args = {"cookies": [{"category": "advertising"}, {"category": "analytics"}],
            "third_party_count": 3, "long_lived_count": 2}
    assert len({_score(**args) for _ in range(25)}) == 1


def test_score_is_capped_at_100():
    assert _score(cookies=[{"category": "advertising"}] * 60, third_party_count=40,
                  long_lived_count=30, has_data_form=True) == 100


def test_advertising_weighs_more_than_analytics():
    assert _score(cookies=[{"category": "advertising"}]) > _score(cookies=[{"category": "analytics"}])


def test_form_without_consent_adds_materially():
    assert _score(has_data_form=True, form_consent_checkbox=False) - _score(has_data_form=False) == 25


def test_missing_banner_and_policy_both_penalised():
    assert _score(has_cookie_banner=True, has_privacy_policy=True) == 0
    assert _score(has_cookie_banner=False, has_privacy_policy=False) == 40


# ---------------------------------------------------------------------------
# Summary -- string-built, leads with recipients and lifetimes
# ---------------------------------------------------------------------------
def test_summary_leads_with_third_parties_and_lifetime():
    s = scoring.build_summary({
        "has_cmp": False, "third_party_domains": ["doubleclick.net", "facebook.net"],
        "max_tracker_lifetime_days": 399, "long_lived_tracker_count": 10,
        "tracking_cookie_count": 25, "cookies_total": 27,
        "has_cookie_banner": False, "has_privacy_policy": True,
        "has_data_form": False, "form_consent_checkbox": False,
    })
    assert s.startswith("Personal data is shared with 2 third-party domains")
    assert "399 days" in s
    assert "25 of 27" in s


def test_summary_never_calls_a_functional_cookie_a_tracker():
    """Real regression: a site with two cookies, neither a tracker, was summarised as
    having "the longest-lived tracker persists for 364 days" -- because lifetime stats
    were taken over all cookies rather than trackers only."""
    cookies = [
        detect.build_cookie_record(
            {"name": "PHPSESSID", "domain": "site.com", "expires": -1}, "site.com"),
        detect.build_cookie_record(
            {"name": "wp_settings", "domain": "site.com", "expires": time.time() + 364 * 86400},
            "site.com"),
    ]
    max_life, long_lived = detect.tracker_lifetime_stats(cookies)
    assert (max_life, long_lived) == (None, 0)

    s = scoring.build_summary({
        "has_cmp": False, "third_party_domains": [],
        "max_tracker_lifetime_days": max_life, "long_lived_tracker_count": long_lived,
        "tracking_cookie_count": 0, "cookies_total": 2,
        "has_cookie_banner": False, "has_privacy_policy": False,
        "has_data_form": False, "form_consent_checkbox": False,
    })
    assert "tracker persists" not in s
    assert "0 of 2" in s


def test_tracker_lifetime_stats_ignores_functional_cookies():
    now = time.time()
    cookies = [
        detect.build_cookie_record({"name": "wp_settings", "domain": "site.com",
                                    "expires": now + 900 * 86400}, "site.com"),
        detect.build_cookie_record({"name": "_ga", "domain": "site.com",
                                    "expires": now + 200 * 86400}, "site.com"),
    ]
    max_life, long_lived = detect.tracker_lifetime_stats(cookies)
    assert max_life == pytest.approx(199, abs=2)   # the 900-day functional one is ignored
    assert long_lived == 1


def test_summary_for_cmp_site():
    s = scoring.build_summary({"has_cmp": True, "cmp_name": "OneTrust"})
    assert "OneTrust" in s


def test_summary_when_clean():
    s = scoring.build_summary({
        "has_cmp": False, "third_party_domains": [], "max_cookie_lifetime_days": None,
        "long_lived_count": 0, "tracking_cookie_count": 0, "cookies_total": 0,
        "has_cookie_banner": True, "has_privacy_policy": True,
        "has_data_form": False, "form_consent_checkbox": False,
    })
    assert "No pre-consent tracking" in s
