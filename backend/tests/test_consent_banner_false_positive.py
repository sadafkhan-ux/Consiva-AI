"""A consent banner is something you click, not a word on the page.

WHY THIS EXISTS
---------------
From an external validation run against news.ycombinator.com (2026-09-24) -- a forum
with no cookie banner, no consent UI of any kind, and zero matches for
cookie/consent/accept/reject/gdpr/privacy in its homepage HTML:

    consent_signal: mechanism_type="banner", confidence=0.5,
                    detection_source="text_keywords",
                    matched_keywords=["agree", "reject all", "preferences"]

    -> high-priority finding: "A consent banner is present but the Reject control is
       not automatable... potentially violating the requirement to allow Data
       Principals to exercise their rights easily."

The validator traced the "agree" match to its source: an ordinary user comment reading
"...both parties agree on this." The detector was keyword-matching the concatenated
visible text of all 25 crawled pages, so any content-heavy site using ordinary English
would report a consent banner that does not exist -- and the analysis step would then
escalate that into a specific, confident finding about a Reject control nobody could
ever find.

For compliance software that is the worst available failure: not a missed issue, but a
fabricated one that sends a privacy team hunting for a UI element that was never there.
"""

import pytest
from bs4 import BeautifulSoup

from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.page_parser import extract_control_text, parse_page


def _detect(control_text: str, **kw):
    return detect_consent_signal(
        scripts=[], visible_text="", detected_global_vars=[], control_text=control_text, **kw
    )


# ── The reported false positive ─────────────────────────────────────────────────

HN_LIKE = """
<html><body>
  <a href="news">Hacker News</a> <a href="newest">new</a> <a href="front">past</a>
  <a href="ask">ask</a> <a href="show">show</a> <a href="jobs">jobs</a>
  <a href="submit">submit</a> <a href="login">login</a>
  <table><tr><td><span class="commtext">
    I think both parties agree on this, though preferences vary and you can
    customize the workflow however you like.
  </span></td></tr></table>
</body></html>
"""


def test_forum_prose_no_longer_reports_a_consent_banner():
    """The bug, as one assertion."""
    page = parse_page(HN_LIKE, "https://news.ycombinator.com/")
    assert _detect(page.control_text).mechanism_type == "none"


def test_the_prose_really_does_contain_the_trigger_words():
    """Guards the test above from passing vacuously. If the fixture stopped containing
    "agree"/"preferences"/"customize", it would pass while proving nothing."""
    page = parse_page(HN_LIKE, "https://news.ycombinator.com/")
    for word in ("agree", "preferences", "customize"):
        assert word in page.visible_text.lower(), f"fixture no longer contains {word!r}"
    assert "agree" not in page.control_text, "prose leaked into the control text"


ARTICLE_ABOUT_COOKIE_BANNERS = """
<html><body>
  <a href="/">Home</a> <a href="/archive">Archive</a>
  <article><p>
    Under GDPR a compliant cookie banner must let visitors reject all non-essential
    cookies as easily as they accept all of them. Many sites still bury the reject
    all control two clicks deep behind cookie settings.
  </p></article>
</body></html>
"""


def test_an_article_about_cookie_banners_is_not_a_cookie_banner():
    """Isolates the input-scoping fix specifically.

    The HN case is now caught twice over -- by scoping to controls AND by demoting
    "agree"/"preferences" to weak hints -- so it cannot show which change mattered.
    This page puts the STRONGEST phrases ("reject all", "accept all", "cookie
    settings") in ordinary prose, where only the scoping can save it: a publication
    writing about consent law is not itself showing a consent banner.
    """
    page = parse_page(ARTICLE_ABOUT_COOKIE_BANNERS, "https://privacyblog.test/gdpr")
    # Fed the old way -- the whole page's rendered text -- it reads as a banner.
    assert detect_consent_signal(
        scripts=[], visible_text=page.visible_text, detected_global_vars=[],
    ).mechanism_type == "banner"
    # Scoped to what a visitor can actually click, it correctly reads as nothing.
    assert _detect(page.control_text).mechanism_type == "none"


# ── What must still be detected ─────────────────────────────────────────────────

REAL_BANNER = """
<html><body>
  <p>We use cookies to improve your experience and to show you relevant ads.</p>
  <div id="cookie-consent-banner">
    <button>Accept all</button>
    <button>Reject all</button>
    <a href="#">Manage preferences</a>
  </div>
</body></html>
"""


def test_a_real_banner_is_still_detected():
    page = parse_page(REAL_BANNER, "https://shop.test/")
    signal = _detect(page.control_text)
    assert signal.mechanism_type == "banner"
    assert signal.has_reject_all is True
    assert signal.has_granular_choices is True


def test_a_banner_whose_buttons_are_divs_is_still_detected():
    """Plain <div onclick> banners are common; the consent-container hint catches them
    so sloppy markup cannot defeat detection."""
    html = """<html><body><div class="cookie-notice">
        <div>Accept all cookies</div><div>Reject all</div></div></body></html>"""
    assert _detect(extract_control_text(BeautifulSoup(html, "lxml"))).mechanism_type == "banner"


def test_button_labels_in_attributes_count():
    """<input type="button" value="Accept all"> carries its label in an attribute."""
    html = '<html><body><input type="button" value="Accept all cookies"></body></html>'
    assert _detect(extract_control_text(BeautifulSoup(html, "lxml"))).mechanism_type == "banner"


# ── Weak words cannot establish a banner on their own ───────────────────────────

@pytest.mark.parametrize("controls", [
    "home settings preferences profile logout",     # an account nav
    "customize your build add to cart",             # a product configurator
    "i agree",                                      # a terms checkbox, not consent UI
    "ok got it",                                    # a dismissible tooltip
])
def test_ordinary_controls_do_not_establish_a_consent_mechanism(controls):
    assert _detect(controls).mechanism_type == "none"


@pytest.mark.parametrize("strong", ["accept all", "reject all"])
def test_a_strong_phrase_alone_does_establish_one(strong):
    assert _detect(strong).mechanism_type == "banner"


# clyzo.com (16 Nov 2026): a footer "Cookie settings" / "Manage cookies" link on a site that shows no banner was reported as "Cookie banner detected, no reject-all, per-purpose
# choices". A settings link alone is not a banner.
@pytest.mark.parametrize("controls", [
    "home about us contact us cookie settings privacy policy",
    "home about manage cookies terms",
    "home privacy preferences login",
    "book a demo cookie settings",                  # "ok" inside "book" and "cookie" must not count as an accepting word
])
def test_a_lone_cookie_settings_link_is_not_a_banner(controls):
    signal = _detect(controls)
    assert signal.mechanism_type == "none"
    assert signal.has_reject_all is None and signal.has_granular_choices is None
    assert signal.evidence["cookie_settings_link_only"] is True


@pytest.mark.parametrize("controls", ["we use cookies cookie settings accept", "cookie settings got it", "manage cookies allow", "cookie preferences ok"])
def test_a_settings_link_beside_an_accepting_control_is_a_banner(controls):
    assert _detect(controls).mechanism_type == "banner"


def test_weak_words_refine_a_banner_once_one_is_established():
    """"preferences" says nothing by itself, but alongside "Reject all" it is the
    granular-choice control."""
    assert _detect("reject all preferences").has_granular_choices is True


# ── The record must not overstate a 0.5 signal ──────────────────────────────────

def test_a_keyword_banner_says_it_needs_confirmation():
    """A 0.5-confidence, no-vendor signal was being read downstream as established
    fact. The record now states its own weakness, in the payload the model reads."""
    signal = _detect("accept all reject all")
    assert signal.evidence["confidence"] == 0.5
    assert signal.evidence["detection_source"] == "control_text_keywords"
    assert "POSSIBLE" in signal.evidence["caveat"]
    assert signal.cmp_vendor is None, "a vendor must never be guessed from keywords"


def test_nothing_detected_is_reported_as_nothing():
    signal = _detect("")
    assert signal.mechanism_type == "none"
    assert signal.evidence["confidence"] == 0.0


# ── The fallback path stays intact ──────────────────────────────────────────────

def test_visible_text_is_still_used_when_no_control_text_is_supplied():
    """Keeps every existing caller working; the scanner passes control_text, but the
    signature must not break anything that does not."""
    assert detect_consent_signal(
        scripts=[], visible_text="accept all reject all", detected_global_vars=[],
    ).mechanism_type == "banner"


# A consent dialog that is in the markup but switched off is not something a visitor can accept or refuse.
@pytest.mark.parametrize("html", [
    '<html><body><div class="cookie-banner" style="display:none"><button>Accept all</button><button>Reject all</button></div></body></html>',
    '<html><body><div id="cookie-consent" hidden><button>Accept all cookies</button></div></body></html>',
    '<html><body><div style="visibility: hidden"><a href="#">Cookie settings</a> <button>Got it</button></div></body></html>',
])
def test_a_consent_dialog_that_is_hidden_in_the_markup_is_not_a_banner(html):
    assert _detect(extract_control_text(BeautifulSoup(html, "lxml"))).mechanism_type == "none"


def test_the_same_dialog_without_the_hiding_is_a_banner():
    html = '<html><body><div class="cookie-banner"><button>Accept all</button><button>Reject all</button></div></body></html>'
    assert _detect(extract_control_text(BeautifulSoup(html, "lxml"))).mechanism_type == "banner"


def test_an_app_download_popup_that_uses_cookie_code_is_not_a_banner():
    """clyzo.com: a first-visit 'Download our App' modal whose script calls getCookie/setCookie."""
    html = ('<html><body><div id="welcomePopup" class="modal fade show"><h5>Download our App</h5><p>Available on Google Play and the App Store</p><a href="#">Google Play</a><a href="#">App Store</a></div>'
            '<script>function getCookie(n){} function setCookie(n){} /* Only show if cookie doesn\'t exist */</script><footer><a href="/cookiepolicy">Cookies</a></footer></body></html>')
    assert _detect(extract_control_text(BeautifulSoup(html, "lxml"))).mechanism_type == "none"


# ── Pop-ups that use accept/reject words but are not about cookies (tested 17 Nov 2026) ───────────────────────────────────────────────────────

def _page(html: str):
    return _detect(extract_control_text(BeautifulSoup("<html><body>" + html + "</body></html>", "lxml")))


@pytest.mark.parametrize("html", [
    '<div class="push-popup"><p>Stay updated</p><button>Allow all notifications</button><button>Block</button></div>',
    '<div class="email-preferences"><p>Marketing emails</p><button>Accept all</button><button>Reject all</button></div>',
    '<div class="friends"><p>You have new friend requests</p><button>Accept all</button><button>Reject all</button></div>',
    '<div class="privacy-wrapper"><button>Accept all friend requests</button></div>',
    '<div class="subscribe-box"><p>Subscribe to our newsletter</p><button>Accept all</button><button>Decline all</button></div>',
])
def test_a_popup_that_is_not_about_cookies_is_not_a_cookie_banner(html):
    assert _page(html).mechanism_type == "none"


@pytest.mark.parametrize("html", [
    '<div id="cookie-banner"><p>We use cookies.</p><button>Accept all</button><button>Reject all</button><a>Manage preferences</a></div>',
    '<div class="notice"><p>We use cookies and tracking to show you ads.</p><button>Accept all</button><button>Reject all</button></div>',
    '<div class="privacy-notice"><p>Your privacy matters. We use cookies.</p><button>Accept all</button><button>Reject all</button></div>',
    '<div class="cookie-notice"><p>This site uses cookies to improve your experience.</p><button>Accept</button><button>Decline</button></div>',
    '<div class="modal"><p>Manage your data</p><button>Accept all cookies</button><button>Reject non-essential</button></div>',
])
def test_real_cookie_banners_are_still_detected_after_the_context_check(html):
    assert _page(html).mechanism_type == "banner"


@pytest.mark.parametrize("phrase,text,expected", [
    ("allow all", "allow all notifications", False),
    ("allow all", "allow all cookies", True),
    ("accept all", "accept all reject all", True),
    ("accept all", "accept all e-mails", False),
    ("reject all", "reject all", True),
])
def test_a_phrase_ending_in_all_is_refused_when_it_names_something_that_is_not_a_cookie(phrase, text, expected):
    from app.scanner.consent_signal_detector import _has
    assert _has(text, phrase) is expected
