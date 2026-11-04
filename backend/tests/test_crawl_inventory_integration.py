"""The URL inventory and the crawl controls inside the real crawl loop (a fake browser, no network), and the profiles that decide whether controls apply."""

import dataclasses

from app.config import get_settings
from app.scanner.crawl_controls import CrawlControls, ResourceGuard
from app.scanner.crawler import _crawl_full_site
from app.scanner.scan_profiles import (
    APPLICATION,
    WEB,
    application_crawl_options,
    profile_for_scan_options,
    resolve_profile,
)
from tests.test_scanner_coverage import ROOT, _FakeContext, _links, _site

S = get_settings()


def _app_profile(**crawl):
    """An application profile carrying the given controls, built the way the worker builds it from the stored scan options."""
    return profile_for_scan_options({"profile": APPLICATION, "crawl": application_crawl_options(crawl)}, S)


async def _run(pages, profile, disallowed=None, sitemap=None, failures=None):
    context = _FakeContext(pages, failures)
    out = await _crawl_full_site(context, ROOT, S, disallowed_prefixes=disallowed or set(), sitemap_urls=sitemap, profile=profile, deadline_monotonic=None)
    diagnostics = out[-1]
    return context, diagnostics, {r.normalized_url: r for r in diagnostics["url_inventory_records"]}


def _chain(n):
    """A site that is one long chain: / -> /d1 -> /d2 -> ..., so depth is the only thing that limits it."""
    pages = {f"{ROOT}/": _links("/d1")}
    for i in range(1, n):
        pages[f"{ROOT}/d{i}"] = _links(f"/d{i + 1}")
    pages[f"{ROOT}/d{n}"] = _links()
    return pages


def by_path(inv):
    return {u.replace(ROOT, "") or "/": r for u, r in inv.items()}


# ── every discovered URL has a record, with the right status and depth ──────────────────────────────

async def test_the_inventory_has_one_record_per_discovered_url_with_depth_and_status():
    _ctx, diag, inv = await _run(_site(4), _app_profile())
    rec = by_path(inv)
    assert len(rec) == 4
    assert rec["/"].crawl_depth == 0 and rec["/"].discovery_source == "seed"
    assert all(rec[f"/p{i}"].crawl_depth == 1 and rec[f"/p{i}"].discovery_source == "link" for i in (1, 2, 3))
    assert all(r.scan_status in ("COMPLETED", "PARTIALLY_SCANNED") for r in rec.values())
    assert all(r.scan_started_at and r.scan_completed_at and r.http_status == 200 for r in rec.values())
    assert diag["url_inventory_counts"]["PENDING"] == 0


async def test_depth_follows_the_link_chain():
    _ctx, _d, inv = await _run(_chain(5), _app_profile())
    rec = by_path(inv)
    assert [rec["/"].crawl_depth] + [rec[f"/d{i}"].crawl_depth for i in range(1, 6)] == [0, 1, 2, 3, 4, 5]


async def test_a_sitemap_url_is_one_step_from_the_entry_and_a_page_found_both_ways_keeps_both_sources():
    pages = {f"{ROOT}/": _links("/a"), f"{ROOT}/a": _links(), f"{ROOT}/s": _links()}
    _ctx, _d, inv = await _run(pages, _app_profile(), sitemap=[f"{ROOT}/a", f"{ROOT}/s"])
    rec = by_path(inv)
    assert rec["/s"].discovery_source == "sitemap" and rec["/s"].crawl_depth == 1
    assert rec["/a"].discovery_source == "sitemap" and rec["/a"].discovery_sources == ["sitemap", "link"]
    assert len(rec) == 3


async def test_spellings_of_one_page_are_one_inventory_record():
    pages = {f"{ROOT}/": _links("/a", "/a/", "/a#top"), f"{ROOT}/a": _links()}
    _ctx, _d, inv = await _run(pages, _app_profile())
    assert len(inv) == 2


async def test_a_failed_page_is_failed_with_its_reason_and_is_never_completed():
    pages = {f"{ROOT}/": _links("/gone", "/ok"), f"{ROOT}/ok": _links()}
    _ctx, _d, inv = await _run(pages, _app_profile(), failures={f"{ROOT}/gone": RuntimeError("HTTP 404")})
    rec = by_path(inv)
    assert rec["/gone"].scan_status == "FAILED" and rec["/gone"].failure_reason and rec["/gone"].final_url is None
    assert rec["/ok"].scan_status in ("COMPLETED", "PARTIALLY_SCANNED")


async def test_a_robots_disallowed_url_is_excluded_with_the_reason_and_never_loaded():
    pages = {f"{ROOT}/": _links("/private/x", "/a"), f"{ROOT}/a": _links()}
    ctx, _d, inv = await _run(pages, _app_profile(), disallowed={"/private"})
    rec = by_path(inv)
    assert (rec["/private/x"].scan_status, rec["/private/x"].status_reason) == ("EXCLUDED", "robots_disallowed")
    assert not any("private" in n for n in ctx.navigations)


# ── crawl depth ──────────────────────────────────────────────────────────────────────────────────────

async def test_max_depth_stops_the_crawl_at_that_level_and_records_what_it_left_out():
    ctx, diag, inv = await _run(_chain(6), _app_profile(max_depth=2))
    rec = by_path(inv)
    assert [rec[p].scan_status for p in ("/", "/d1", "/d2")] != ["EXCLUDED"] * 3
    assert rec["/d3"].scan_status == "EXCLUDED" and rec["/d3"].status_reason == "depth_limit" and rec["/d3"].crawl_depth == 3
    assert len(ctx.navigations) == 3 and diag["pages_excluded_depth_limit"] == 1
    assert "/d4" not in rec          # never discovered: its only link is on a page that was never loaded


async def test_depth_zero_scans_only_the_entry_address():
    ctx, _d, inv = await _run(_site(5), _app_profile(max_depth=0))
    rec = by_path(inv)
    assert len(ctx.navigations) == 1
    assert all(r.scan_status == "EXCLUDED" and r.status_reason == "depth_limit" for p, r in rec.items() if p != "/")


async def test_with_no_depth_limit_the_whole_chain_is_crawled():
    ctx, _d, _inv = await _run(_chain(8), _app_profile())
    assert len(ctx.navigations) == 9


# ── maximum URL count ────────────────────────────────────────────────────────────────────────────────

async def test_max_urls_caps_the_inventory_and_the_scan_says_how_many_were_not_recorded():
    ctx, diag, inv = await _run(_site(30), _app_profile(max_urls=10))
    assert len(inv) == 10 and len(ctx.navigations) == 10
    assert diag["discovery_limit"] == {"max_urls": 10, "urls_not_recorded": 20}


async def test_duplicates_do_not_use_up_the_url_limit():
    pages = {f"{ROOT}/": _links("/a", "/a/", "/a#x", "/b"), f"{ROOT}/a": _links(), f"{ROOT}/b": _links()}
    _ctx, diag, inv = await _run(pages, _app_profile(max_urls=3))
    assert len(inv) == 3 and diag["discovery_limit"]["urls_not_recorded"] == 0


# ── discovered is not scanned ────────────────────────────────────────────────────────────────────────

async def test_pages_the_crawl_never_reached_stay_pending_with_the_reason():
    profile = dataclasses.replace(_app_profile(), max_pages=3)           # an explicit page budget, as CONSENT_APP_MAX_PAGES would set
    _ctx, diag, inv = await _run(_site(12), profile)
    states = [r.scan_status for r in inv.values()]
    assert states.count("PENDING") > 0
    assert all(r.status_reason == "page_limit" for r in inv.values() if r.scan_status == "PENDING")
    assert all(r.scan_started_at is None for r in inv.values() if r.scan_status == "PENDING")
    assert diag["url_inventory_counts"]["PENDING"] == states.count("PENDING")


# ── concurrency ──────────────────────────────────────────────────────────────────────────────────────

async def test_the_scan_never_runs_more_pages_at_once_than_its_effective_concurrency():
    import asyncio

    active = peak = 0
    base = _FakeContext(_site(12))
    real_new_page = base.new_page

    async def new_page():
        page = await real_new_page()
        real_goto = page.goto

        async def goto(*a, **k):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            try:
                return await real_goto(*a, **k)
            finally:
                active -= 1

        page.goto = goto
        return page

    base.new_page = new_page
    profile = _app_profile(concurrency=3, per_domain_concurrency=2)
    await _crawl_full_site(base, ROOT, S, disallowed_prefixes=set(), profile=profile, deadline_monotonic=None)
    assert 1 <= peak <= 2


# ── rate limit ───────────────────────────────────────────────────────────────────────────────────────

async def test_navigation_starts_are_rate_limited():
    import time

    profile = _app_profile(requests_per_second=20, concurrency=8)
    started = time.monotonic()
    ctx, _d, _inv = await _run(_site(9), profile)
    elapsed = time.monotonic() - started
    assert len(ctx.navigations) == 9 and elapsed >= (9 - 1) / 20 - 0.02        # nine starts at 20 a second take at least eight intervals


# ── resource budget ──────────────────────────────────────────────────────────────────────────────────

async def test_a_scan_over_its_memory_budget_stops_launching_and_says_so(monkeypatch):
    from app.scanner import crawler

    class AlwaysOver(ResourceGuard):
        def __init__(self, budget_mb, **kw):
            super().__init__(budget_mb, reader=lambda: 99_999.0)

    monkeypatch.setattr(crawler, "ResourceGuard", AlwaysOver)
    ctx, diag, _inv = await _run(_site(6), _app_profile(memory_budget_mb=1024))
    assert diag["stop_reason"] == "resource_limit" or len(ctx.navigations) == 0
    assert any(e["event"] == "throttled" for e in diag["resource_events"])


async def test_a_scan_under_its_budget_is_not_throttled(monkeypatch):
    from app.scanner import crawler

    class Fine(ResourceGuard):
        def __init__(self, budget_mb, **kw):
            super().__init__(budget_mb, reader=lambda: 10.0)

    monkeypatch.setattr(crawler, "ResourceGuard", Fine)
    ctx, diag, _inv = await _run(_site(5), _app_profile(memory_budget_mb=1024))
    assert len(ctx.navigations) == 5 and diag["resource_events"] == []


# ── the public profile is unchanged ──────────────────────────────────────────────────────────────────

async def test_the_web_profile_still_scans_at_most_five_pages_and_has_no_controls():
    ctx = _FakeContext(_site(40))
    profile = resolve_profile(WEB, S)
    *_r, diag = await _crawl_full_site(ctx, ROOT, S, disallowed_prefixes=set(), profile=profile, deadline_monotonic=None)
    assert len(ctx.navigations) == 5 and diag["crawl_controls"] is None
    assert len(diag["url_inventory_records"]) <= 5000            # even a public scan's inventory is bounded


async def test_controls_stored_on_a_web_scan_are_ignored():
    stored = {"profile": WEB, "crawl": application_crawl_options({"max_urls": 1000, "concurrency": 16})}
    profile = profile_for_scan_options(stored, S)
    ctx = _FakeContext(_site(40))
    *_r, diag = await _crawl_full_site(ctx, ROOT, S, disallowed_prefixes=set(), profile=profile, deadline_monotonic=None)
    assert len(ctx.navigations) == 5 and diag["crawl_controls"] is None


async def test_an_old_application_scan_with_no_controls_runs_as_before():
    profile = profile_for_scan_options({"profile": APPLICATION}, S)
    ctx = _FakeContext(_site(8))
    *_r, diag = await _crawl_full_site(ctx, ROOT, S, disallowed_prefixes=set(), profile=profile, deadline_monotonic=None)
    assert len(ctx.navigations) == 8 and diag["crawl_controls"] is None and diag["discovery_limit"]["max_urls"] == 5000
    assert diag["pages_scanned"] == 8


async def test_the_application_profile_crawls_beyond_five_pages_within_its_controls():
    ctx, diag, _inv = await _run(_site(12), _app_profile(max_urls=100))
    assert diag["pages_scanned"] == 12 and len(ctx.navigations) == 12
    assert isinstance(_app_profile().crawl, CrawlControls)


async def test_a_redirect_to_another_website_is_excluded_with_the_reason_and_the_other_address_is_never_recorded():
    from tests.test_five_page_cap import _RedirectContext

    pages = {f"{ROOT}/": _links("/out"), f"{ROOT}/out": _links()}
    ctx = _RedirectContext(pages, {f"{ROOT}/out": "https://elsewhere.test/landing"})
    *_r, diag = await _crawl_full_site(ctx, ROOT, S, disallowed_prefixes=set(), profile=_app_profile(), deadline_monotonic=None)
    rec = {r.original_url.replace(ROOT, "") or "/": r for r in diag["url_inventory_records"]}
    assert (rec["/out"].scan_status, rec["/out"].status_reason, rec["/out"].final_url) == ("EXCLUDED", "redirected_to_another_site", None)
    assert "elsewhere" not in str([r.model_dump() for r in diag["url_inventory_records"]])
