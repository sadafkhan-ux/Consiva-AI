"""Seeding a scan's frontier: the entry page, robots.txt, and the site's own sitemap(s).

Reuses the 5-page scanner's robots reader and sitemap reader as they are (read-only imports), so the Enterprise crawl obeys exactly the same rules about what a site allows: robots.txt
is read as RFC 9309 says, an unreadable one stops the scan unless the deployment says otherwise, and sitemap entries get no wider scope than a link would.
"""

from __future__ import annotations

import uuid

from playwright.async_api import BrowserContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.crawl import frontier, scope
from app.scanner.crawler import _fetch_robots          # existing, unchanged: robots.txt per RFC 9309 with the SSRF checks
from app.scanner.robots import RobotsRules
from app.scanner.sitemap import _MAX_URLS as SITEMAP_CAP          # read-only: the shared reader's ceiling, so the scan can report that it was reached
from app.scanner.sitemap import discover_sitemap_urls
from app.scanner.url_safety import assert_safe_url


async def read_robots(context: BrowserContext, root_url: str) -> RobotsRules:
    """The rules that apply to the scanner. Raises the existing RobotsUnreadable when robots.txt cannot be read and the deployment says not to guess."""
    await assert_safe_url(root_url)
    rules, _sitemaps = await _fetch_robots(context, root_url)
    return rules


async def seed(db: AsyncSession, scan_id: uuid.UUID, context: BrowserContext, root_url: str, rules: RobotsRules, *, max_urls: int, max_depth: int) -> dict[str, int]:
    """Put the entry page (depth 0) and every sitemap URL (depth 1) into the frontier. Returns how many of each kind were recorded.

    Only the exact submitted host is followed (crawl/host.py): a sitemap file on another host is never fetched, and a sitemap entry on another host is RECORDED as excluded
    (`excluded_subdomain` / `excluded_external_domain`) with its reason, so the inventory shows what was found and why it was not scanned."""
    out = {"seed": 0, "sitemap": 0, "sitemap_excluded": 0, "not_recorded": 0, "off_host": 0}
    result = await frontier.add_url(db, scan_id, root_url, depth=0, source="seed", max_urls=max_urls, max_depth=max_depth)
    out["seed"] = 1 if result == frontier.ADDED else 0
    off_host: list[str] = []

    def in_scope(u: str) -> bool:
        ok = scope.same_site(u, root_url)
        if not ok and scope.classify(u, root_url) in scope.OFF_HOST:
            off_host.append(u)                       # not fetched, not crawled: recorded below
        return ok

    urls = await discover_sitemap_urls(context, root_url, list(rules.sitemaps), in_scope)
    # The 5-page sitemap reader (shared, unchanged) stops at SITEMAP_CAP URLs. Say so when it did: entries past the cap are found only if a page links to them (phase 1).
    out["sitemap_listed"], out["sitemap_capped"] = len(urls), int(len(urls) >= SITEMAP_CAP)
    for i, url in enumerate(urls):
        kind = scope.classify(url, root_url)
        if kind == scope.SKIP:
            continue
        reason = scope.exclusion_reason(url, root_url, context="listed in the sitemap") if kind in scope.OFF_HOST else scope.EXCLUDE_REASONS.get(kind)
        result = await frontier.add_url(db, scan_id, url, depth=1, source="sitemap", max_urls=max_urls, max_depth=max_depth, exclude_reason=reason)
        if result == frontier.ADDED:
            out["sitemap"] += 1
        elif result == frontier.EXCLUDED:
            out["sitemap_excluded"] += 1
        elif result == frontier.URL_LIMIT:
            # The rest of the sitemap cannot be recorded either; count it so the scan can say how many URLs it dropped.
            out["not_recorded"] = sum(1 for u in urls[i:] if scope.classify(u, root_url) != scope.SKIP)
            break
    for url in dict.fromkeys(off_host):
        result = await frontier.add_url(db, scan_id, scope.clean(url), depth=1, source="sitemap", max_urls=max_urls, max_depth=max_depth,
                                        exclude_reason=scope.exclusion_reason(url, root_url, context="named by the sitemap or robots.txt"))
        if result == frontier.URL_LIMIT:
            break
        out["off_host"] += 1 if result == frontier.EXCLUDED else 0
    return out


async def add_links(db: AsyncSession, scan_id: uuid.UUID, root_url: str, links: list[str], *, parent_depth: int, max_urls: int, max_depth: int) -> dict:
    """Record the links found on a page, one level deeper. Pages on the submitted host are queued; documents and query variants are recorded as excluded with their reason; a link to a
    subdomain or another website is recorded as excluded (`excluded_subdomain` / `excluded_external_domain`), never queued; anything that is not a web page (mailto:, images) is ignored.
    `added_urls` lists the URLs newly queued, in page order."""
    out: dict = {"added": 0, "excluded": 0, "duplicate": 0, "limit": 0, "off_host": 0, "added_urls": []}
    for i, link in enumerate(links):
        kind = scope.classify(link, root_url)
        if kind == scope.SKIP:
            continue
        off = kind in scope.OFF_HOST
        reason = scope.exclusion_reason(link, root_url, context="linked from a scanned page") if off else scope.EXCLUDE_REASONS.get(kind)
        result = await frontier.add_url(
            db, scan_id, scope.clean(link), depth=parent_depth + 1, source="link", max_urls=max_urls, max_depth=max_depth, exclude_reason=reason)
        if result == frontier.ADDED:
            out["added"] += 1
            out["added_urls"].append(scope.clean(link))
        elif result in (frontier.EXCLUDED, frontier.DEPTH_LIMIT):
            out["excluded"] += 1
            out["off_host"] += 1 if off else 0
        elif result == frontier.DUPLICATE:
            out["duplicate"] += 1
        else:
            out["limit"] = sum(1 for u in links[i:] if scope.classify(u, root_url) != scope.SKIP)      # none of the rest can be recorded
            break
    return out
