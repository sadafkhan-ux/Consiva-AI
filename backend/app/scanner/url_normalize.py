"""Canonical form + crawl priority for a discovered URL.

Exists because the BFS in crawler.py keyed its `seen_urls` set on the raw absolute
href produced by page_parser.parse_page(). That made four spellings of one page --
`/about`, `/about/`, `/about#team`, `/about?utm_source=nav` -- four distinct queue
entries, each one consuming a slot of the page budget to re-fetch bytes already
fetched. On a real site whose nav carries UTM tags or whose footer links to
fragment anchors, most of a 25-page budget could be spent that way, which is a large
part of why raising the cap alone would not have bought proportionally more coverage.

Normalisation here is deliberately CONSERVATIVE about what it treats as the same
page. Anything that can change what a server returns is preserved: the path (beyond
a trailing slash), and every query parameter that is not on the known
client-side-only tracking list below. `?id=5` and `?page=2` are different pages and
stay different; `?utm_source=x` is not.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that are advertising/analytics attribution only: they are read by
# client-side tag managers, never by the origin server to select content. Stripping
# them merges the many tagged spellings of one page into one crawl entry. Deliberately
# an explicit allow-list of known-inert names rather than a pattern -- an unknown
# parameter is assumed to be content-bearing and kept, so the failure mode is "crawled
# one page twice", not "never crawled a real page".
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "gclid", "gclsrc", "dclid", "wbraid", "gbraid",  # Google Ads
    "fbclid",                                         # Meta
    "msclkid",                                        # Microsoft Ads / Bing UET
    "twclid",                                         # X/Twitter
    "igshid",                                         # Instagram
    "ttclid",                                         # TikTok
    "li_fat_id",                                      # LinkedIn
    "mc_cid", "mc_eid",                               # Mailchimp
    "_hsenc", "_hsmi", "hsa_acc", "hsa_cam",          # HubSpot
    "yclid",                                          # Yandex
    "s_kwcid", "ef_id",                               # Adobe / generic paid search
    "piwik_campaign", "pk_campaign", "pk_kwd",        # Matomo
    "ref", "referrer", "source",                      # common hand-rolled equivalents
})

_DEFAULT_PORTS = {"http": "80", "https": "443"}

# Crawl order. This scanner's product is a CONSENT/privacy audit, so "important" is
# scored by evidence value for that audit specifically, not by generic site
# importance: a privacy or cookie policy settles R-006/R-007 outright, and a page
# carrying a real form (signup/contact/checkout) is what R-004 needs. Those are worth
# reaching before the 40th blog post when a budget is finite.
#
# This is ADDITIVE to BFS, not a replacement for it: the queue is still breadth-first
# over genuinely discovered links, and these weights only reorder what is already in
# it. No path is ever synthesised or requested speculatively -- a site that has no
# /privacy simply never gets one queued, and nothing here fabricates a 404.
_PRIORITY_PATTERNS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("privacy", "cookie", "gdpr", "dpdp", "data-protection", "datenschutz")),
    (1, ("terms", "legal", "disclaimer", "imprint")),
    (2, ("signup", "sign-up", "register", "login", "sign-in", "signin",
         "checkout", "cart", "payment", "subscribe", "contact")),
    (3, ("about", "pricing", "plans", "product", "service", "solution")),
    # 4 = anything not matched below (the default in `priority`)
    (5, ("blog", "news", "article", "post", "tag", "category", "archive", "author")),
)


def normalize_url(url: str) -> str:
    """Canonical spelling of `url`, for dedup and for the crawl queue's identity key.

    Drops the fragment (never sent to a server, so it cannot select different
    content), lowercases scheme and host (both case-insensitive per RFC 3986 while
    the path is not), drops a redundant default port, strips the tracking parameters
    above while preserving order-insensitivity for the rest, and removes a trailing
    slash from a non-root path. Returns the input unchanged if it cannot be parsed --
    a URL this cannot understand is passed through rather than mangled into something
    that resolves somewhere else.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url

    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    netloc = hostname
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{hostname}:{parts.port}"

    # keep_blank_values: `?q=` is a real, different request from `?` absent.
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    # Sorted so `?a=1&b=2` and `?b=2&a=1` -- the same request to every server that
    # parses a query string as a mapping -- collapse to one crawl entry.
    query = urlencode(sorted(kept))

    path = parts.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def priority(url: str) -> int:
    """Lower sorts earlier. The seed/root page is 0 -- it is the page both interaction
    passes re-visit and the one most likely to carry the consent banner."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return 4
    path = (parts.path or "/").lower()
    if path in ("", "/"):
        return 0
    haystack = f"{path}?{(parts.query or '').lower()}"
    for rank, needles in _PRIORITY_PATTERNS:
        if any(needle in haystack for needle in needles):
            return rank
    return 4
