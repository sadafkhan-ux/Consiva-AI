"""Which pages the crawl reaches first. A page is judged by what it IS (a legal page, a form page), not by a keyword somewhere in a long article slug."""

import pytest

from app.scanner.url_normalize import priority
from tests.test_navigation_resilience import quick  # noqa: F401

BASE = "https://shop.example"


@pytest.mark.parametrize("path", [
    "/blog/dpdp-act-2023-compliance-guide-indian-enterprises",     # seen on swaransoft.com: the slug says "dpdp", the page is a blog post
    "/blog/how-to-write-a-privacy-policy",
    "/news/cookie-law-update-2026",
    "/articles/gdpr-for-small-business",
    "/insights/terms-and-conditions-explained",
    "/posts/2026/10/contact-us-tips",
    "/press/legal-update",
    "/category/privacy/page-2",
])
def test_an_article_about_a_topic_is_not_the_page_for_that_topic(path):
    assert priority(BASE + path) == 5, path


@pytest.mark.parametrize("path,rank", [
    ("/privacy", 0), ("/privacy-policy", 0), ("/legal/privacy", 0), ("/cookie-policy", 0), ("/cookies", 0), ("/dpdp", 0),
    ("/terms", 1), ("/terms-of-use", 1), ("/legal", 1),
    ("/contact", 2), ("/contact-us", 2), ("/login", 2), ("/checkout", 2), ("/subscribe", 2),
    ("/about", 3), ("/pricing", 3), ("/services", 3),
    ("/random-page", 4),
])
def test_the_pages_the_audit_needs_keep_their_rank(path, rank):
    assert priority(BASE + path) == rank, path


def test_the_contact_page_is_reached_before_a_blog_post_that_mentions_privacy():
    assert priority(BASE + "/contact") < priority(BASE + "/blog/dpdp-act-2023-compliance-guide")


def test_a_path_that_only_contains_the_word_blog_inside_another_word_is_not_a_blog():
    assert priority(BASE + "/blogger-relations-contact") == 2
    assert priority(BASE + "/newsroom-contact") == 2


def test_the_front_door_is_still_first():
    assert priority(BASE + "/") == 0


class TestSections:
    def test_the_section_is_the_first_directory(self):
        from app.scanner.url_normalize import section_of
        assert section_of("https://k.example/in-en/index.html") == "in-en"
        assert section_of("https://k.example/in-en/") == "in-en"
        assert section_of("https://k.example/in-en/about/team") == "in-en"
        assert section_of("https://k.example/") == ""
        assert section_of("https://k.example/index.html") == ""
        assert section_of("https://k.example/privacy") == ""            # a page, not a section

    async def test_a_scan_of_one_section_does_not_spend_its_pages_on_the_same_page_of_other_sections(self, quick):  # noqa: F811
        """konicaminolta.com/in-en/: four of the five scanned pages were /contact in OTHER countries' sections."""
        from app.config import get_settings
        from app.scanner.crawler import _crawl_full_site
        from tests.test_navigation_resilience import SiteContext, links

        seed = "https://k.example/in-en/index.html"
        foreign = [f"/{c}/contact/index.html" for c in ("ae-en", "africa2-en", "ar-es", "asiapacific4-en")]
        own = ["/in-en/contact/index.html", "/in-en/about/index.html"]
        site = {seed: ("ok", 200, links(*foreign, *own))}
        ctx = SiteContext(site)
        await _crawl_full_site(ctx, seed, get_settings(), disallowed_prefixes=set())
        visited = [u for u in ctx.visited if u != seed]
        assert "https://k.example/in-en/contact/index.html" in visited[:2], visited
        assert visited[:2] != [f"https://k.example{foreign[0]}", f"https://k.example{foreign[1]}"], "foreign contact pages must not take the first slots"

    async def test_a_privacy_page_in_another_section_still_comes_first(self, quick):  # noqa: F811
        from app.config import get_settings
        from app.scanner.crawler import _crawl_full_site
        from tests.test_navigation_resilience import SiteContext, links

        seed = "https://k.example/in-en/index.html"
        site = {seed: ("ok", 200, links("/in-en/contact/index.html", "/privacy-policy"))}
        ctx = SiteContext(site)
        await _crawl_full_site(ctx, seed, get_settings(), disallowed_prefixes=set())
        assert next(u for u in ctx.visited if u != seed) == "https://k.example/privacy-policy"
