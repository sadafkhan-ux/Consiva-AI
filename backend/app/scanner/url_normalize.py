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

import re
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
    (1, ("terms",)),
    # Contact and the legal-notice pages (who the site is and how to reach it) before the pages where a visitor signs in or pays. Within this rank a page with a shorter path comes first
    # and then the alphabetical order of the address, so /contact is reached before /disclaimer, /imprint and /legal.
    (2, ("contact", "legal", "disclaimer", "imprint")),
    (3, ("signup", "sign-up", "register", "login", "sign-in", "signin", "checkout", "cart", "payment", "subscribe")),
    (4, ("about", "pricing", "plans", "product", "service", "solution")),
    # 5 = anything not matched below (the default in `priority`)
    (6, ("blog", "news", "article", "post", "tag", "category", "archive", "author")),
)


# A path with one of these as a whole segment is editorial content (a post, an article, a press item), whatever its slug says. The slug names the TOPIC of the content, not what the page
# is: /blog/dpdp-act-2023-compliance-guide matched "dpdp" and outranked the contact page, so a scan of swaransoft.com spent one of its five pages on a blog post and never loaded /contact.
_CONTENT_SECTIONS = frozenset({
    "blog", "blogs", "news", "article", "articles", "post", "posts", "insights", "press", "stories", "magazine",
    "tag", "tags", "category", "categories", "archive", "archives", "author", "authors",
})


def normalize_url(url: str) -> str:
    """Canonical spelling of `url`, for dedup and for the crawl queue's identity key.

    Drops the fragment (never sent to a server, so it cannot select different
    content), lowercases scheme and host (both case-insensitive per RFC 3986 while
    the path is not), folds a leading "www." away, drops a redundant default port, strips the tracking parameters
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
    # `www.example.com/` and `example.com/` are one page of one site (the crawler already treats them as the same main domain). Keyed apart,
    # they took two of the scan's five pages for the same content: seen live on swaransoft.com, where the apex and the www homepage were both scanned.
    if hostname.startswith("www.") and hostname.count(".") >= 2:
        hostname = hostname[4:]
    netloc = hostname
    try:
        port = parts.port
    except ValueError:
        # `https://example.com:99999/` or `:abc`: a link on a page can carry anything. One bad link must not fail a finished crawl, so the address is
        # returned as it came (the crawler then fails to load it, which is recorded against that one page).
        return url
    if port and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{hostname}:{port}"

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


def section_of(url: str) -> str:
    """The first directory of a URL's path ("in-en" for https://x.com/in-en/index.html), or "" for a front-door or file path. Large sites run one section per country or language under one host,
    and an entered address inside one of them (/in-en/) is a request to look at that section."""
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return ""
    segments = [s for s in path.lower().split("/") if s]
    if len(segments) < 2 and not path.endswith("/"):
        return ""                      # "/index.html" or "/privacy": a file or a page, not a section
    return segments[0] if segments and "." not in segments[0] else ""


def priority(url: str) -> int:
    """Lower sorts earlier. The seed/root page is 0 -- it is the page both interaction
    passes re-visit and the one most likely to carry the consent banner."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return 5
    path = (parts.path or "/").lower()
    if path in ("", "/"):
        return 0
    if any(segment in _CONTENT_SECTIONS for segment in path.split("/")):
        return 6
    haystack = f"{path}?{(parts.query or '').lower()}"
    for rank, needles in _PRIORITY_PATTERNS:
        if any(needle in haystack for needle in needles):
            return rank
    return 5


# The names a site gives its policy pages, and the directories it keeps them in. A page called /privacy-policy, /privacy-and-cookies or /legal/anything is the site's own policy
# page; a product or marketing page that merely contains the word ("/dpdp-and-gdpr-consent", "/privacy-blueprint", "/cookie-consent-management") is not, and must not take a
# slot from it.
_POLICY_SLUG = re.compile(
    r"^(?:privacy|cookie|cookies|data-protection|data-privacy|gdpr|dpdp|datenschutz|terms|legal|contact|contactus|disclaimer|imprint|tos)"
    r"(?:-(?:policy|notice|statement|settings|preferences|centre|center|us|conditions|rights|choices|of-use|of-service|and-(?:cookies?|privacy|terms|conditions)(?:-(?:policy|notice))?))*$"
)
_POLICY_DIRECTORIES = frozenset({"legal", "policies", "policy", "terms", "privacy"})


def is_policy_page(url: str) -> bool:
    """True when the address is named like a policy page or lives in the site's legal area: its last path segment (without a file extension) is one of the usual names, or a
    directory above it is /legal/, /policies/ or the like."""
    try:
        path = (urlsplit(normalize_url(url)).path or "/").lower()
    except ValueError:
        return False
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return False
    last = segments[-1].rsplit(".", 1)[0]
    return bool(_POLICY_SLUG.match(last)) or any(d in _POLICY_DIRECTORIES for d in segments[:-1])


#: Where a page whose address only contains a privacy, terms or contact word ranks: after the site's own policy, terms and contact pages (0 to 2), before the sign-up pages (3).
_KEYWORD_ONLY_RANK = 2.5


def effective_rank(url: str) -> float:
    """The rank the crawl orders a page by: `priority`, except that a page in ranks 0 to 2 (privacy and cookie, terms, contact and legal notice) whose address is not named like a
    policy page ranks `_KEYWORD_ONLY_RANK`. "/privacy-blueprint" says something about privacy; it is not the privacy policy."""
    rank = priority(url)
    return _KEYWORD_ONLY_RANK if rank <= 2 and not is_policy_page(url) and urlsplit(url).path not in ("", "/") else float(rank)


def tie_break_key(url: str) -> tuple[int, int, str]:
    """What orders two pages of the same rank: the shallower path first, then an address without a query string before one with it, then the address itself.
    It reads only the address, so the order does not depend on which page or sitemap entry mentioned a link first, or on which page finished loading first: the same
    site gives the same order on every scan."""
    key = normalize_url(url)
    try:
        parts = urlsplit(key)
    except ValueError:
        return (99, 1, url)
    depth = len([seg for seg in (parts.path or "").split("/") if seg])
    return (depth, 1 if parts.query else 0, key)


# A path ending in one of these is a script that picks its content from the query string (/index.php?page=privacy), so its query is part of the page's identity.
_SCRIPT_SUFFIXES = (".php", ".asp", ".aspx", ".jsp", ".jspx", ".cgi", ".cfm", ".do", ".action")


def path_without_query(url: str) -> str:
    """The address with its query string and fragment removed (the identity of the page a variant is a variant of)."""
    try:
        parts = urlsplit(normalize_url(url))
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def query_variant_of(url: str) -> str | None:
    """The address without its query string, when `url` is only a query-string variant of a page that has a path of its own (/blog?page=2, /products?sort=price),
    else None. The root address and a script's address are never variants: /?p=12 and /index.php?id=3 are different pages, because the query is what selects them."""
    try:
        parts = urlsplit(normalize_url(url))
    except ValueError:
        return None
    path = parts.path or "/"
    if not parts.query or path == "/" or path.lower().endswith(_SCRIPT_SUFFIXES) or "/index." in path.lower():
        return None
    return path_without_query(url)


_REASON_BY_RANK = {
    0: "Privacy or cookie page: ranked first, because it settles the notice and cookie checks.",
    1: "Terms page: ranked after the privacy and cookie pages.",
    2: "Contact or legal-notice page: says who the site is and how to reach it.",
    3: "Sign-up, login or checkout page: where a visitor gives personal data.",
    4: "About, product or service page.",
    5: "Other public page of the site.",
    6: "Blog or news page: ranked last, because it adds the least to a privacy review.",
}


def selection_reason(url: str, discovered_via: str | None) -> str | None:
    """Why this page was chosen, in one sentence: its kind (the rank the crawler orders pages by) and where the crawler found it. None for a page record that is not a
    chosen page (an attempt that failed keeps the reason it failed, which is its status)."""
    if discovered_via == "seed":
        return "The address that was entered: always scanned first."
    if discovered_via not in ("link", "sitemap"):
        return None
    found = "found in the sitemap" if discovered_via == "sitemap" else "found through a link on a scanned page"
    kind = "A page whose address mentions privacy, cookies, terms or contact but is not named like a policy page: ranked after the site's own policy pages." if effective_rank(url) == _KEYWORD_ONLY_RANK else _REASON_BY_RANK[priority(url)]
    return f"{kind} Selected from pages {found}."
