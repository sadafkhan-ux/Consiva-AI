"""Which five pages a scan chooses, and why. The selection is one function of the site's own public addresses: the same site gives the same pages on every scan, the order depends
on what a page is (privacy and cookie pages first, blog posts last) and not on which link was met first, a repeat of an address is not scanned again, and each page chosen carries its reason."""

import pytest
from playwright.async_api import Error as PlaywrightError

from app.config import get_settings
from app.scanner.crawler import _crawl_full_site
from app.scanner.url_normalize import (
    effective_rank,
    is_policy_page,
    path_without_query,
    priority,
    query_variant_of,
    selection_reason,
    tie_break_key,
)
from tests.test_navigation_resilience import SiteContext, links, quick  # noqa: F401

ROOT = "https://x.test/"
S = get_settings().model_copy(update={"consent_web_max_pages": 5, "scanner_max_pages": 5})


async def crawl(site):
    ctx = SiteContext(site)
    pages, *_rest, diagnostics = await _crawl_full_site(ctx, ROOT, S, disallowed_prefixes=set())
    return ctx, pages, diagnostics


class TestTheOrderOfSamePriorityPages:
    def test_a_shallower_address_comes_before_a_deeper_one_and_a_plain_one_before_a_query_variant(self):
        keys = {u: tie_break_key(u) for u in ("https://x.test/a/b/c", "https://x.test/a/b", "https://x.test/a?x=1", "https://x.test/a", "https://x.test/b")}
        order = sorted(keys, key=keys.get)
        assert order == ["https://x.test/a", "https://x.test/b", "https://x.test/a?x=1", "https://x.test/a/b", "https://x.test/a/b/c"]


    @pytest.mark.parametrize("url,policy", [
        ("https://x.test/privacy", True), ("https://x.test/privacy-policy", True), ("https://x.test/cookie-policy.html", True), ("https://x.test/legal/privacy-rights-and-grievance", True),
        ("https://x.test/policies/anything", True), ("https://x.test/terms-of-use", True), ("https://x.test/contact-us", True),
        ("https://x.test/dpdp-and-gdpr-consent", False), ("https://x.test/privacy-blueprint", False), ("https://x.test/cookie-consent-management", False), ("https://x.test/dpdpa/data-fiduciary-obligations", False),
        ("https://x.test/", False),
    ])
    def test_a_policy_named_page_is_told_from_a_page_that_only_contains_the_word(self, url, policy):
        assert is_policy_page(url) is policy

    async def test_the_sites_own_policy_page_beats_marketing_pages_that_mention_privacy(self, quick):  # noqa: F811
        """uat.consiva.ai: four product pages (/dpdp-and-gdpr-consent, /privacy-blueprint ...) took the slots and the real policy, /legal/privacy-rights-and-grievance, was not scanned."""
        site = {ROOT: ("ok", 200, links("/dpdp-and-gdpr-consent", "/privacy-blueprint", "/cookie-consent-management", "/dpdpa/data-fiduciary-obligations", "/legal/privacy-rights-and-grievance", "/legal/terms"))}
        ctx, *_ = await crawl(site)
        chosen = {u.removeprefix("https://x.test") for u in ctx.visited if u != ROOT}
        assert "/legal/privacy-rights-and-grievance" in chosen and "/legal/terms" in chosen, chosen
        assert chosen == {"/legal/privacy-rights-and-grievance", "/legal/terms", "/cookie-consent-management", "/dpdp-and-gdpr-consent"}, "the two real policy pages first, then the shallowest of the rest"

    @pytest.mark.parametrize("path,rank", [
        ("/privacy", 0), ("/privacy-and-cookies", 0), ("/legal/privacy-rights-and-grievance", 0), ("/terms", 1), ("/contact", 2),
        ("/privacy-blueprint", 2.5), ("/dpdp-and-gdpr-consent", 2.5), ("/contact-center-software", 2.5), ("/login", 3), ("/about", 4), ("/blog/x", 6),
    ])
    def test_a_page_that_only_contains_the_word_ranks_after_the_sites_own_policy_pages(self, path, rank):
        assert effective_rank("https://x.test" + path) == rank

    def test_the_reason_for_such_a_page_does_not_call_it_the_privacy_page(self):
        r = selection_reason("https://x.test/privacy-blueprint", "link")
        assert "not named like a policy page" in r and "ranked first" not in r

    def test_the_key_reads_only_the_address(self):
        assert tie_break_key("https://x.test/a#frag") == tie_break_key("https://x.test/a")
        assert tie_break_key("https://x.test/a?utm_source=n") == tie_break_key("https://x.test/a")

    async def test_the_same_site_gives_the_same_pages_whatever_order_its_links_are_met_in(self, quick):  # noqa: F811
        paths = ["/zeta", "/alpha/deep", "/beta", "/gamma", "/alpha", "/delta", "/eps"]
        a, _, _ = await crawl({ROOT: ("ok", 200, links(*paths))})
        b, _, _ = await crawl({ROOT: ("ok", 200, links(*reversed(paths)))})
        c, _, _ = await crawl({ROOT: ("ok", 200, links(*sorted(paths)))})
        assert set(a.visited) == set(b.visited) == set(c.visited), (a.visited, b.visited, c.visited)
        # of seven pages of one kind and a budget of five, the four chosen after the entered address are the shallowest, then the alphabetical first
        assert {u.removeprefix("https://x.test") for u in a.visited if u != ROOT} == {"/alpha", "/beta", "/delta", "/eps"}

    async def test_the_same_site_gives_the_same_pages_whether_its_pages_come_from_the_sitemap_or_from_links(self, quick):  # noqa: F811
        from app.scanner.crawler import _crawl_full_site as crawl_full
        site = {ROOT: ("ok", 200, links("/b", "/a"))}
        ctx1, ctx2 = SiteContext(site), SiteContext(site)
        await crawl_full(ctx1, ROOT, S, disallowed_prefixes=set(), sitemap_urls=["https://x.test/c", "https://x.test/a"])
        await crawl_full(ctx2, ROOT, S, disallowed_prefixes=set(), sitemap_urls=["https://x.test/a", "https://x.test/c"])
        assert ctx1.visited == ctx2.visited


class TestWhichPagesAreChosen:
    async def test_the_entered_address_then_privacy_terms_and_contact_before_anything_else(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/blog/a-post", "/about", "/random", "/contact", "/terms", "/cookie-policy", "/privacy", "/products"))}
        ctx, _pages, d = await crawl(site)
        chosen = [u.removeprefix("https://x.test") or "/" for u in ctx.visited]
        assert chosen[0] == "/"
        assert set(chosen[1:]) == {"/privacy", "/cookie-policy", "/terms", "/contact"}, chosen
        assert d["pages_scanned"] == 5 and d["pages_discovered"] == 9 and d["stop_reason"] == "page_limit"

    async def test_the_contact_page_is_chosen_before_a_second_legal_notice_or_a_login_page(self, quick):  # noqa: F811
        """swaransoft.com: privacy, cookie policy, terms and a disclaimer took all four slots, and the contact page was never reached."""
        site = {ROOT: ("ok", 200, links("/disclaimer", "/imprint", "/login", "/checkout", "/contact", "/privacy", "/terms", "/cookie-policy", "/about"))}
        ctx, *_ = await crawl(site)
        chosen = {u.removeprefix("https://x.test") for u in ctx.visited if u != ROOT}
        assert chosen == {"/privacy", "/cookie-policy", "/terms", "/contact"}, chosen

    async def test_a_blog_post_about_privacy_does_not_take_a_slot_from_the_contact_page(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/blog/dpdp-act-2023-compliance-guide", "/contact", "/privacy", "/terms", "/about", "/random"))}
        ctx, *_ = await crawl(site)
        assert "https://x.test/blog/dpdp-act-2023-compliance-guide" not in ctx.visited
        assert "https://x.test/contact" in ctx.visited

    async def test_a_page_that_cannot_be_scanned_is_recorded_with_its_reason_and_the_next_suitable_page_is_chosen(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/privacy", "/terms", "/contact", "/about", "/random", "/more")), "https://x.test/privacy": ("error", PlaywrightError("net::ERR_CONNECTION_RESET"))}
        _ctx, pages, d = await crawl(site)
        failed = [p for p in pages if p.discovered_via == "failed"]
        assert [p.url for p in failed] == ["https://x.test/privacy"]
        scanned = [p.url for p in pages if p.discovered_via not in ("failed", "timeout")]
        assert len(scanned) == 5 and "https://x.test/privacy" not in scanned
        assert d["pages_failed"] == 1 and d["pages_scanned"] == 5
        assert "https://x.test/about" in scanned, "the next page in order takes the place of the one that did not load"

    async def test_the_scan_limit_is_not_raised(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links(*[f"/p{i}" for i in range(30)]))}
        _ctx, _pages, d = await crawl(site)
        assert d["pages_scanned"] == 5 and d["page_limit"] == 5
        assert d["pages_discovered"] == 31 and d["pages_not_attempted_budget_exhausted"] >= 25

    async def test_nothing_is_guessed(self, quick):  # noqa: F811
        ctx, *_ = await crawl({ROOT: ("ok", 200, links("/about"))})
        assert ctx.visited == [ROOT, "https://x.test/about"], "no /privacy, /terms or /contact is requested when nothing links to it"


class TestRepeatsOfAnAddress:
    async def test_fragments_and_tracking_parameters_are_one_page(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/privacy", "/privacy#top", "/privacy?utm_source=nav", "/privacy/"))}
        ctx, _pages, d = await crawl(site)
        assert [u for u in ctx.visited if "privacy" in u] == ["https://x.test/privacy"]
        assert d["pages_discovered"] == 2

    async def test_a_repeated_query_string_variant_of_a_page_is_not_scanned_again(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/pricing", "/pricing?plan=a", "/pricing?plan=b", "/blog?page=2", "/blog?page=3", "/about"))}
        ctx, _pages, d = await crawl(site)
        visited = [u.removeprefix("https://x.test") for u in ctx.visited]
        assert visited.count("/pricing") == 1 and not any(u.startswith("/pricing?") for u in visited), visited
        assert sum(1 for u in visited if u.startswith("/blog")) == 1, "one blog address, not one per page number"
        assert d["skipped_by_reason"]["query_variant_of_known_page"] == 3

    async def test_a_plain_address_wins_over_its_variant_even_when_the_variant_is_met_first(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/privacy?lang=fr", "/privacy", "/about"))}
        ctx, *_ = await crawl(site)
        assert "https://x.test/privacy" in ctx.visited and "https://x.test/privacy?lang=fr" not in ctx.visited

    async def test_pages_whose_content_is_chosen_by_the_query_are_different_pages(self, quick):  # noqa: F811
        site = {ROOT: ("ok", 200, links("/index.php?page=privacy", "/index.php?page=terms", "/?p=12", "/?p=13"))}
        ctx, *_ = await crawl(site)
        for u in ("/index.php?page=privacy", "/index.php?page=terms", "/?p=12", "/?p=13"):
            assert "https://x.test" + u in ctx.visited, u

    @pytest.mark.parametrize("url,variant_of", [
        ("https://x.test/blog?page=2", "https://x.test/blog"), ("https://x.test/a/b?sort=price", "https://x.test/a/b"),
        ("https://x.test/privacy", None), ("https://x.test/?p=12", None), ("https://x.test/index.php?id=3", None), ("https://x.test/app.aspx?x=1", None),
        ("https://x.test/blog?utm_source=x", None),     # a tracking parameter is not part of the address at all
    ])
    def test_which_addresses_are_variants(self, url, variant_of):
        assert query_variant_of(url) == variant_of

    def test_the_address_of_a_variants_page(self):
        assert path_without_query("https://www.x.test/a/b?x=1&y=2#f") == "https://x.test/a/b"


class TestEachPageCarriesItsReason:
    @pytest.mark.parametrize("path,rank,words", [
        ("/privacy", 0, "Privacy or cookie page"), ("/cookie-policy", 0, "Privacy or cookie page"), ("/terms", 1, "Terms page"),
        ("/contact", 2, "Contact or legal-notice page"), ("/disclaimer", 2, "Contact or legal-notice page"), ("/login", 3, "Sign-up, login or checkout"),
        ("/about", 4, "About, product or service"), ("/random", 5, "Other public page"), ("/blog/post", 6, "Blog or news page"),
    ])
    def test_the_reason_names_the_kind_of_page_and_where_it_was_found(self, path, rank, words):
        assert priority("https://x.test" + path) == rank
        for via, found in (("sitemap", "found in the sitemap"), ("link", "found through a link on a scanned page")):
            r = selection_reason("https://x.test" + path, via)
            assert words in r and found in r, r

    def test_the_entered_address_says_it_is_scanned_first(self):
        assert selection_reason("https://x.test/", "seed") == "The address that was entered: always scanned first."

    def test_an_attempt_that_did_not_load_has_no_reason_of_its_own(self):
        assert selection_reason("https://x.test/privacy", "failed") is None
        assert selection_reason("https://x.test/privacy", "timeout") is None
        assert selection_reason("https://x.test/privacy", None) is None

    def test_the_evidence_the_api_returns_carries_it_and_the_agents_own_evidence_does_not(self):
        from types import SimpleNamespace

        from app.db.repositories.scan_repository import _serialize_evidence

        pages = [SimpleNamespace(id=1, url="https://x.test/privacy", title="P", http_status=200, discovered_via="sitemap"), SimpleNamespace(id=2, url="https://x.test/x", title=None, http_status=None, discovered_via="failed")]
        detailed = _serialize_evidence(pages, [], [], [], [], [], [], include_cookie_detail=True)["pages"]
        assert "Privacy or cookie page" in detailed[0]["selection_reason"] and detailed[1]["selection_reason"] is None
        assert all("selection_reason" not in p for p in _serialize_evidence(pages, [], [], [], [], [], [])["pages"])
