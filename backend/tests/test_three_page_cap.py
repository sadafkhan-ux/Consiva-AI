"""A public scan covers at most THREE distinct pages, however it is configured or driven.

Why this exists: scans capped at 5 reported 12 or more "pages". The budget only ever counted navigations, but every link
that robots.txt disallowed was also appended to the pages list as a record of its own, with no limit -- so a site with
many disallowed links produced many "pages". Those links are skipped pages, not pages; they are now reported with their
reason and are never page records.

The limit is enforced where pages are launched (the crawl), not only in settings: a larger configured value is clamped,
redirects cannot bring a page in twice, parallel workers share one budget, and third-party requests are never pages.
"""

import dataclasses

import pytest

from app.config import Settings, get_settings
from app.scanner.crawler import _crawl_full_site
from app.scanner.scan_profiles import APPLICATION, MAX_WEB_PAGES, WEB, resolve_profile
from app.scanner.url_normalize import normalize_url
from tests.test_scanner_coverage import ROOT, _FakeContext, _FakePage, _crawl, _links, _site


class _RedirectPage(_FakePage):
    def __init__(self, pages, failures, requests, *, redirects):
        super().__init__(pages, failures, requests)
        self._redirects = redirects
        self._final = None

    async def goto(self, url, timeout=None, wait_until=None):
        response = await super().goto(url, timeout=timeout, wait_until=wait_until)
        self._final = self._redirects.get(normalize_url(url))
        return response

    @property
    def url(self):
        return self._final or self._url


class _RedirectContext(_FakeContext):
    def __init__(self, pages, redirects):
        super().__init__(pages)
        self.redirects = {normalize_url(k): v for k, v in redirects.items()}

    async def new_page(self):
        return _RedirectPage(self.pages, self.failures, self.navigations, redirects=self.redirects)


def test_the_ceiling_is_three():
    assert MAX_WEB_PAGES == 3
    assert get_settings().scanner_max_pages == 3 and get_settings().consent_web_max_pages == 3


@pytest.mark.parametrize("field,value", [("scanner_max_pages", 5), ("scanner_max_pages", 500), ("consent_web_max_pages", 25)])
def test_a_larger_configured_limit_is_clamped_to_three_not_honoured_and_not_a_startup_failure(field, value):
    settings = Settings.model_validate({**get_settings().model_dump(), field: value})

    assert getattr(settings, field) == 3


async def test_the_crawl_enforces_three_even_if_every_configured_limit_says_more():
    context = _FakeContext(_site(40))
    settings = get_settings().model_copy(update={"scanner_max_pages": 500})            # bypasses the settings clamp
    profile = dataclasses.replace(resolve_profile(WEB, settings), max_pages=25)        # and a profile that says 25

    *_rest, diagnostics = await _crawl_full_site(
        context, ROOT, settings, disallowed_prefixes=set(), profile=profile, deadline_monotonic=None,
    )

    assert len(context.navigations) == 3 and len(set(context.navigations)) == 3
    assert diagnostics["pages_attempted"] == 3 and diagnostics["page_limit"] == 3


async def test_parallel_workers_share_one_budget():
    context = _FakeContext(_site(40))
    settings = get_settings().model_copy(update={"scanner_max_concurrent_pages": 10})

    *_rest, diagnostics = await _crawl(context, settings)

    assert len(context.navigations) == 3 and diagnostics["pages_attempted"] == 3


async def test_the_application_profile_is_a_different_mode_and_is_not_capped_at_three():
    context = _FakeContext(_site(8))
    settings = get_settings().model_copy(update={"consent_app_max_pages": 0, "consent_app_max_seconds": 3600})

    *_rest, diagnostics = await _crawl_full_site(
        context, ROOT, settings, disallowed_prefixes=set(), profile=resolve_profile(APPLICATION, settings),
        deadline_monotonic=None,
    )

    assert diagnostics["pages_scanned"] == 8


# ── robots-disallowed links are skipped pages, never page records ───────────────────────────

async def test_many_robots_disallowed_links_never_become_pages():
    links = [f"/private/{i}" for i in range(15)] + ["/a", "/b"]
    pages = {f"{ROOT}/": _links(*links), f"{ROOT}/a": _links(), f"{ROOT}/b": _links()}
    context = _FakeContext(pages)

    records, *_rest, diagnostics = await _crawl(context, disallowed={"/private"})

    assert len(records) == 3, "root, /a and /b -- not the 15 disallowed links"
    assert diagnostics["pages_scanned"] == 3
    assert diagnostics["skipped_by_reason"]["robots_disallowed"] == 15
    assert diagnostics["pages_blocked_robots"] == 15


async def test_every_discovered_page_is_either_scanned_or_has_a_recorded_reason():
    links = ["/privacy-policy", "/terms", "/about", "/about/", "/x", "/private/1", "/private/2"]
    pages = {f"{ROOT}/": _links(*links)}
    for path in ("/privacy-policy", "/terms", "/about", "/x"):
        pages[f"{ROOT}{path}"] = _links()
    context = _FakeContext(pages)

    _records, *_rest, d = await _crawl(context, disallowed={"/private"})

    reasons = d["skipped_by_reason"]
    assert d["pages_discovered"] == d["pages_attempted"] + reasons["page_limit"] + reasons["robots_disallowed"]
    assert reasons["duplicate_url"] == 1, "/about/ is the same page as /about"
    assert {s["reason"] for s in d["skipped_pages"]} == {"page_limit", "robots_disallowed", "duplicate_url"}
    assert all(s["url"] for s in d["skipped_pages"])


async def test_the_skipped_list_is_capped_but_the_counts_are_complete():
    links = [f"/private/{i}" for i in range(120)]
    context = _FakeContext({f"{ROOT}/": _links(*links)})

    *_rest, d = await _crawl(context, disallowed={"/private"})

    assert d["skipped_by_reason"]["robots_disallowed"] == 120
    assert len(d["skipped_pages"]) == 50


# ── redirects ────────────────────────────────────────────────────────────────────────────────

async def test_a_redirect_to_an_already_scanned_page_is_counted_once():
    pages = {f"{ROOT}/": _links("/old", "/real"), f"{ROOT}/old": _links(), f"{ROOT}/real": _links()}
    context = _RedirectContext(pages, {f"{ROOT}/old": f"{ROOT}/real"})   # /old redirects to /real: one page, requested twice

    records, *_rest, d = await _crawl(context)

    urls = [p.url for p in records]
    assert len([u for u in urls if u.endswith("/real")]) + len([u for u in urls if u.endswith("/old")]) == 1
    assert d["pages_attempted"] <= 3
    assert d["skipped_by_reason"].get("redirected_to_already_scanned_page") == 1


async def test_a_redirect_to_another_site_is_not_a_page_of_this_one():
    pages = {f"{ROOT}/": _links("/leaving", "/stay"), f"{ROOT}/leaving": _links(), f"{ROOT}/stay": _links()}
    context = _RedirectContext(pages, {f"{ROOT}/leaving": "https://elsewhere.test/landing"})

    records, *_rest, d = await _crawl(context)

    assert not [p for p in records if "elsewhere" in p.url]
    assert d["skipped_by_reason"] == {"redirected_off_site": 1}


# ── main domain first, third parties are not pages ───────────────────────────────────────────────

async def test_third_party_links_are_not_candidate_pages():
    context = _FakeContext({f"{ROOT}/": _links("https://cdn.thirdparty.test/lib.js", "https://ads.other.test/")})

    records, *_rest, d = await _crawl(context)

    assert [p.url for p in records] == [ROOT]
    assert d["links_off_site_ignored"] == 2 and d["skipped_pages"] == []


async def test_the_scan_stays_on_the_entered_host_and_lists_subdomains_instead_of_crawling_them():
    pages = {
        f"{ROOT}/": _links("https://blog.example.com/", "https://blog.example.com/post", "/privacy-policy", "/terms"),
        "https://blog.example.com/": _links(), "https://blog.example.com/post": _links(),
        f"{ROOT}/privacy-policy": _links(), f"{ROOT}/terms": _links(),
    }
    context = _FakeContext(pages)

    records, *_rest, d = await _crawl(context)

    assert {p.url for p in records} == {ROOT, f"{ROOT}/privacy-policy", f"{ROOT}/terms"}
    assert not [u for u in context.navigations if "blog.example.com" in u], "a subdomain is never navigated to"
    assert d["discovered_domains"] == [
        {"host": "blog.example.com", "kind": "subdomain", "links": 2, "sample_url": "https://blog.example.com/", "framed": False}]
    assert d["links_subdomain_not_scanned"] == 2
    assert {"url": "https://blog.example.com/", "reason": "subdomain_not_scanned"} in d["skipped_pages"]


async def test_a_subdomain_gets_no_slot_even_when_slots_are_free():
    context = _FakeContext({f"{ROOT}/": _links("https://shop.example.com/"), "https://shop.example.com/": _links()})

    records, *_rest, d = await _crawl(context)

    assert [p.url for p in records] == [ROOT]
    assert d["pages_attempted"] == 1 and d["discovered_domains"][0]["host"] == "shop.example.com"


async def test_external_sites_are_listed_per_host_with_how_often_they_were_linked_and_are_never_pages():
    links = ["https://cdn.thirdparty.test/a", "https://cdn.thirdparty.test/b", "https://ads.other.test/", "/about"]
    context = _FakeContext({f"{ROOT}/": _links(*links), f"{ROOT}/about": _links()})

    records, *_rest, d = await _crawl(context)

    assert {p.url for p in records} == {ROOT, f"{ROOT}/about"}
    by_host = {e["host"]: e for e in d["discovered_domains"]}
    assert by_host["cdn.thirdparty.test"] == {"host": "cdn.thirdparty.test", "kind": "external", "links": 2,
                                              "sample_url": "https://cdn.thirdparty.test/a", "framed": False}
    assert by_host["ads.other.test"]["kind"] == "external"
    assert d["links_off_site_ignored"] == 3 and d["skipped_pages"] == [], "external hosts are not skipped PAGES"


async def test_www_and_the_bare_domain_are_the_same_site():
    context = _FakeContext({f"{ROOT}/": _links("https://www.example.com/about"), "https://www.example.com/about": _links()})

    records, *_rest, d = await _crawl(context)

    assert {p.url for p in records} == {ROOT, "https://www.example.com/about"} and d["discovered_domains"] == []


async def test_a_frame_onto_another_host_is_listed_as_a_framed_domain():
    from tests.test_framed_site import FRAMESET

    context = _FakeContext({f"{ROOT}/": FRAMESET.replace("https://corp.example.org/", "https://corp.example.com/")})

    *_rest, d = await _crawl(context)

    assert d["framed_sites"] == ["https://corp.example.com/"]
    assert d["discovered_domains"] == [{"host": "corp.example.com", "kind": "subdomain", "links": 1,
                                        "sample_url": "https://corp.example.com/", "framed": True}]


async def test_a_redirect_onto_a_subdomain_is_not_a_page_and_the_host_is_listed():
    pages = {f"{ROOT}/": _links("/go", "/stay"), f"{ROOT}/go": _links(), f"{ROOT}/stay": _links()}
    context = _RedirectContext(pages, {f"{ROOT}/go": "https://app.example.com/home"})

    records, *_rest, d = await _crawl(context)

    assert not [p for p in records if "app.example.com" in p.url]
    assert d["skipped_by_reason"] == {"redirected_to_subdomain": 1}
    assert [e["host"] for e in d["discovered_domains"]] == ["app.example.com"]


async def test_the_list_of_other_hosts_is_capped():
    links = [f"https://host{i}.elsewhere.test/" for i in range(160)]
    context = _FakeContext({f"{ROOT}/": _links(*links)})

    *_rest, d = await _crawl(context)

    assert len(d["discovered_domains"]) == 100
