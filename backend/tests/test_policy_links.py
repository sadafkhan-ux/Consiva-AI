"""A policy link is recognised by what it says or is, not only by visible text: an icon link, an aria-labelled link, and the same address linked twice.

Seen live: konicaminolta.com's only privacy link was `<a href="/siteinfo/index.html" class="privacy-notice-link" aria-label="More information about your privacy rights">` with no text, so a scan
of five pages reported no privacy policy at all (and a failed N1 requirement) for a site that has one.
"""

import pytest

from app.scanner.page_parser import parse_page
from app.scanner.policy_detector import detect_policies


def policies(html, base="https://www.kmx.com/in-en/index.html"):
    page = parse_page(html, base)
    return [(p.policy_type, p.url) for p in detect_policies(page.links, {}, "kmx.com")]


class TestRecognisingTheLink:
    def test_an_icon_link_named_by_its_aria_label(self):
        html = '<a href="/siteinfo/index.html" aria-label="More information about your privacy rights"><img src="i.svg"></a>'
        assert policies(html) == [("privacy_policy", "https://www.kmx.com/siteinfo/index.html")]

    def test_an_icon_link_named_by_its_title(self):
        assert policies('<a href="/legal/c" title="Cookie Policy"><svg></svg></a>') == [("cookie_policy", "https://www.kmx.com/legal/c")]

    def test_an_icon_link_named_only_by_its_class(self):
        assert policies('<a href="/siteinfo/index.html" class="privacy-notice-link"><i></i></a>') == [("privacy_policy", "https://www.kmx.com/siteinfo/index.html")]
        assert policies('<a href="/x" id="cookie_policy"></a>') == [("cookie_policy", "https://www.kmx.com/x")]

    def test_the_same_address_linked_twice_keeps_the_occurrence_that_says_something(self):
        html = '<a href="/siteinfo/index.html"><img src="i.svg"></a><a href="/siteinfo/index.html">Privacy Policy</a>'
        assert policies(html) == [("privacy_policy", "https://www.kmx.com/siteinfo/index.html")]

    @pytest.mark.parametrize("html", [
        '<a href="/shop" class="cookie-banner-link">Shop</a>',               # styling that merely mentions a cookie banner
        '<a href="/shop" class="privacy-icon">Shop</a>',
        '<a href="/about"><img src="i.svg"></a>',
        '<a href="/products" aria-label="Our products">Products</a>',
    ])
    def test_ordinary_links_are_not_policies(self, html):
        assert policies(html) == []

    def test_a_policy_on_another_site_is_still_not_this_sites_policy(self):
        assert policies('<a href="https://other.org/privacy" aria-label="Privacy policy"></a>') == []

    def test_visible_text_still_works_as_before(self):
        assert policies('<a href="/p">Privacy Policy</a><a href="/t">Terms and Conditions</a>') == [
            ("privacy_policy", "https://www.kmx.com/p"), ("terms", "https://www.kmx.com/t")]


def test_parse_page_keeps_visible_text_over_the_accessible_name():
    page = parse_page('<a href="/a" aria-label="Aria">Visible</a><a href="/b" aria-label="Only aria"></a>', "https://xsite.com/")
    assert [(link.href, link.text) for link in page.links] == [("https://xsite.com/a", "Visible"), ("https://xsite.com/b", "Only aria")]


def test_a_link_named_as_both_a_cookie_and_a_privacy_policy_is_the_privacy_policy():
    """konicaminolta.com's OneTrust banner: the same address is linked as .ot-cookie-policy-link and .privacy-notice-link, with no text."""
    html = '<a href="/siteinfo/index.html" class="ot-cookie-policy-link"></a><a href="/siteinfo/index.html" class="privacy-notice-link" aria-label="More information about your privacy rights"></a>'
    assert policies(html) == [("privacy_policy", "https://www.kmx.com/siteinfo/index.html")]
