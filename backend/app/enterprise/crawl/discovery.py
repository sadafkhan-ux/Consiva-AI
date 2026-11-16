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
from app.scanner.sitemap import discover_sitemap_urls
from app.scanner.url_safety import assert_safe_url


async def read_robots(context: BrowserContext, root_url: str) -> RobotsRules:
    """The rules that apply to the scanner. Raises the existing RobotsUnreadable when robots.txt cannot be read and the deployment says not to guess."""
    await assert_safe_url(root_url)
    rules, _sitemaps = await _fetch_robots(context, root_url)
    return rules


async def seed(db: AsyncSession, scan_id: uuid.UUID, context: BrowserContext, root_url: str, rules: RobotsRules, *, max_urls: int, max_depth: int) -> dict[str, int]:
    """Put the entry page (depth 0) and every sitemap URL (depth 1) into the frontier. Returns how many of each kind were recorded."""
    out = {"seed": 0, "sitemap": 0, "sitemap_excluded": 0, "not_recorded": 0}
    result = await frontier.add_url(db, scan_id, root_url, depth=0, source="seed", max_urls=max_urls, max_depth=max_depth)
    out["seed"] = 1 if result == frontier.ADDED else 0
    urls = await discover_sitemap_urls(context, root_url, list(rules.sitemaps), lambda u: scope.same_site(u, root_url))
    for i, url in enumerate(urls):
        kind = scope.classify(url, root_url)
        if kind in (scope.SKIP, scope.EXTERNAL):
            continue
        reason = scope.EXCLUDE_REASONS.get(kind)
        result = await frontier.add_url(db, scan_id, url, depth=1, source="sitemap", max_urls=max_urls, max_depth=max_depth, exclude_reason=reason)
        if result == frontier.ADDED:
            out["sitemap"] += 1
        elif result == frontier.EXCLUDED:
            out["sitemap_excluded"] += 1
        elif result == frontier.URL_LIMIT:
            # The rest of the sitemap cannot be recorded either; count it so the scan can say how many URLs it dropped.
            out["not_recorded"] = sum(1 for u in urls[i:] if scope.classify(u, root_url) not in (scope.SKIP, scope.EXTERNAL))
            break
    return out


async def add_links(db: AsyncSession, scan_id: uuid.UUID, root_url: str, links: list[str], *, parent_depth: int, max_urls: int, max_depth: int) -> dict[str, int]:
    """Record the links found on a page, one level deeper. Pages are queued; documents and query variants are recorded as excluded with their reason; the rest are ignored."""
    out = {"added": 0, "excluded": 0, "duplicate": 0, "limit": 0}
    for i, link in enumerate(links):
        kind = scope.classify(link, root_url)
        if kind in (scope.SKIP, scope.EXTERNAL):
            continue
        result = await frontier.add_url(
            db, scan_id, scope.clean(link), depth=parent_depth + 1, source="link", max_urls=max_urls, max_depth=max_depth, exclude_reason=scope.EXCLUDE_REASONS.get(kind))
        if result == frontier.ADDED:
            out["added"] += 1
        elif result in (frontier.EXCLUDED, frontier.DEPTH_LIMIT):
            out["excluded"] += 1
        elif result == frontier.DUPLICATE:
            out["duplicate"] += 1
        else:
            out["limit"] = sum(1 for u in links[i:] if scope.classify(u, root_url) not in (scope.SKIP, scope.EXTERNAL))      # none of the rest can be recorded
            break
    return out
