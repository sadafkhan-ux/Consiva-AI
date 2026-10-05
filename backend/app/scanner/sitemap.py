"""sitemap.xml discovery — a second source of crawlable URLs alongside the BFS.

Why this exists: link-following alone only ever reaches what is linked from a page
the crawl already fetched. Anything reachable solely from a nav the crawler didn't
render, from a paginated index, or from a section the homepage doesn't link to was
invisible no matter how high the page budget went. A sitemap is the site's own
published list of its pages, so it widens discovery without guessing at URLs --
schemas.PageRecord already reserved "sitemap" as a `discovered_via` value for this.

Security posture matches _fetch_robots in crawler.py, for the same reasons:
fetched through `context.request` with `max_redirects=0` so a hostile site cannot 30x
a backend-issued request at an internal/metadata address, every extracted URL is
re-checked against the same-registered-domain rule before it can be queued, and the
whole thing is best-effort -- any failure logs and yields nothing rather than failing
the scan.
"""

import logging
from urllib.parse import urlparse

from lxml import etree
from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)

# Bounds. A sitemap is attacker-controlled input from the scanned site, so every
# dimension of it is capped rather than trusted: total URLs taken, index children
# followed, nesting depth, and the byte size of any single document parsed.
_MAX_URLS = 500  # ceiling on what discovery CONTRIBUTES; the page budget still caps what is fetched
_MAX_INDEX_CHILDREN = 10
_MAX_DEPTH = 2  # a sitemapindex pointing at sitemaps (depth 1) pointing at urlsets (depth 2)
_MAX_BYTES = 10 * 1024 * 1024
_FETCH_TIMEOUT_MS = 8000

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Hardened parser for untrusted XML: no external entity resolution and no network
# access (closes XXE / entity-expansion file and SSRF reads), no DTD load, and
# recover=True so one malformed tag yields the URLs that did parse instead of
# discarding the whole document. huge_tree stays False to keep lxml's own
# billion-laughs and deep-nesting limits in force.
_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False,
    dtd_validation=False, huge_tree=False, recover=True,
)


def _local_name(tag: object) -> str:
    """Tag name without its namespace. Real sitemaps are inconsistent about declaring
    the sitemaps.org namespace -- some omit it, some use a variant URI -- so matching
    on the local name accepts both instead of silently returning nothing."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _extract(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """Returns (page_urls, child_sitemap_urls) from one sitemap document.

    A <urlset> yields page URLs; a <sitemapindex> yields child sitemap URLs. Both are
    collected in one pass rather than branching on the root tag, because `recover=True`
    can hand back a partially-parsed tree whose root is not the one the document
    intended.
    """
    try:
        root = etree.fromstring(xml_bytes, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        logger.info("Could not parse sitemap XML: %s", exc)
        return [], []
    if root is None:
        return [], []

    pages: list[str] = []
    children: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "loc" or not element.text:
            continue
        loc = element.text.strip()
        if not loc:
            continue
        parent_name = _local_name(element.getparent().tag) if element.getparent() is not None else ""
        (children if parent_name == "sitemap" else pages).append(loc)
    return pages, children


async def _fetch(context: BrowserContext, url: str) -> bytes | None:
    try:
        response = await context.request.get(url, timeout=_FETCH_TIMEOUT_MS, max_redirects=0)
        if not response.ok:
            return None
        body = await response.body()
    except Exception as exc:  # noqa: BLE001 — sitemaps are best-effort, never fatal
        logger.info("Could not fetch sitemap %s: %s", url, exc)
        return None
    if len(body) > _MAX_BYTES:
        logger.info("Ignoring sitemap %s: %d bytes exceeds the %d cap", url, len(body), _MAX_BYTES)
        return None
    return body


async def discover_sitemap_urls(
    context: BrowserContext,
    root_url: str,
    robots_sitemaps: list[str],
    same_site: "object",
) -> list[str]:
    """Best-effort list of same-site page URLs published by the site's own sitemap(s).

    `robots_sitemaps` are the `Sitemap:` directives already parsed out of robots.txt
    (free -- that file is fetched regardless), tried before the conventional
    /sitemap.xml fallback. `same_site` is a predicate taking a URL and returning
    whether it is in scope; the registered-domain rule lives in crawler.py and is
    passed in rather than duplicated here.

    Order is preserved and duplicates removed, so a caller's own priority sort stays
    deterministic. Never raises.
    """
    parsed_root = urlparse(root_url)
    candidates: list[str] = []
    for candidate in [*robots_sitemaps, f"{parsed_root.scheme}://{parsed_root.netloc}/sitemap.xml"]:
        if candidate not in candidates:
            candidates.append(candidate)

    found: list[str] = []
    seen: set[str] = set()
    queue = [(url, 1) for url in candidates]
    visited_sitemaps: set[str] = set()

    while queue and len(found) < _MAX_URLS:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in visited_sitemaps or depth > _MAX_DEPTH:
            continue
        visited_sitemaps.add(sitemap_url)
        # A sitemap URL is itself attacker-controlled (robots.txt and sitemapindex both
        # supply them), so it passes the same scope check as any page URL before this
        # process will fetch it.
        if not same_site(sitemap_url):
            logger.info("Ignoring off-site sitemap reference: %s", sitemap_url)
            continue

        body = await _fetch(context, sitemap_url)
        if body is None:
            continue

        pages, children = _extract(body)
        for loc in pages:
            if len(found) >= _MAX_URLS:
                break
            if loc in seen or not same_site(loc):
                continue
            seen.add(loc)
            found.append(loc)
        for child in children[:_MAX_INDEX_CHILDREN]:
            queue.append((child, depth + 1))

    if found:
        logger.info("Sitemap discovery contributed %d same-site URL(s) for %s", len(found), root_url)
    return found
