"""What the Enterprise crawl may follow: pure rules, no browser and no database.

A link is one of: a PAGE to render, a DOCUMENT (PDF, DOCX: fetched and read page by page; other office formats are queued and recorded as not supported), a QUERY VARIANT (the same page with filter or
sort parameters, recorded but not crawled, so a faceted listing cannot create a million URLs), or SKIP (not a page at all: mailto:, images, scripts). Pagination parameters are
the one query a crawl follows, because the next page of a list is a different page.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urldefrag, urlparse

from app.enterprise.crawl import host

PAGE, DOCUMENT, QUERY_VARIANT, SKIP, EXTERNAL, SUBDOMAIN = "page", "document", "query_variant", "skip", "external", "subdomain"
#: Not on the submitted host: recorded as excluded with the reason (crawl/host.py), never crawled.
OFF_HOST = (EXTERNAL, SUBDOMAIN)

DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ppt", ".pptx")
SKIP_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif", ".css", ".js", ".mjs", ".map", ".json", ".xml",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp3", ".mp4", ".webm", ".avi", ".mov", ".zip", ".gz", ".tar", ".rar", ".7z", ".exe", ".dmg", ".apk",
)
#: The only query parameters that name a different page of the same list.
PAGINATION_PARAMS = frozenset({"page", "p", "paged", "pagenumber", "page_no", "pg", "pageno", "offset", "start"})


def clean(url: str) -> str:
    """The URL without its #fragment (a fragment is the same page)."""
    return urldefrag(url.strip())[0]


def same_site(url: str, root_url: str) -> bool:
    """On EXACTLY the submitted host, over http(s) (owner's rule, 19 Nov 2026; crawl/host.py). Subdomains, the www. twin and other websites are not the scanned site. (It was the registered
    domain, the 5-page scanner's boundary; the Enterprise scan is narrower.)"""
    return host.is_same_host(url, root_url)


def exclusion_reason(url: str, root_url: str, *, context: str = "") -> str | None:
    """`excluded_subdomain: ...` or `excluded_external_domain: ...` for a URL that is not on the submitted host; None when it is."""
    return host.exclusion(url, root_url, context=context)


def classify(url: str, root_url: str) -> str:
    parsed = urlparse(clean(url))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return SKIP
    w = host.where(url, root_url)
    if w is None:
        return SKIP
    if w == host.SUBDOMAIN:
        return SUBDOMAIN
    if w == host.EXTERNAL:
        return EXTERNAL
    path = parsed.path.lower()
    if path.endswith(DOCUMENT_EXTENSIONS):
        return DOCUMENT
    if path.endswith(SKIP_EXTENSIONS):
        return SKIP
    keys = {k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if keys and not keys <= PAGINATION_PARAMS:
        return QUERY_VARIANT
    return PAGE


EXCLUDE_REASONS = {
    QUERY_VARIANT: "query_variant: the same page with filter or sort parameters; not crawled",
}


_LEGAL = __import__("re").compile(r"(privacy|cookie|terms|policy|policies|grievance|dpo|data-protection|legal|contact|redress|consent)", __import__("re").I)
_ARTICLE = __import__("re").compile(r"/(blog|blogs|news|article|articles|insights|press|stories|magazine|post|posts)(/|$)", __import__("re").I)


def is_article_path(url: str) -> bool:
    """An address under a blog, news or press section: not asked about when the site's addresses are read for policy pages."""
    return bool(_ARTICLE.search(urlparse(url).path))


_PRIVACY = __import__("re").compile(r"(privacy|data-protection|data_protection|dataprotection|dpdp|personal-data|personal_data)", __import__("re").I)
#: Crawl order (owner's sequence, 19 Nov 2026): the homepage is read first by the runner; then privacy policies, then the other legal pages (cookie, terms, grievance, contact,
#: consent), then every other page.
PRIORITY_PRIVACY, PRIORITY_LEGAL, PRIORITY_OTHER = 0, 1, 2


def legal_priority(url: str) -> int:
    """0 for a privacy policy, 1 for another page a privacy assessment needs early (it names a cookie policy, terms, grievance or contact page), 2 for any other page; judged by the address,
    and never for an article: a blog post that mentions the law is not the policy, so a blog address never earns priority."""
    path = urlparse(url).path
    if _ARTICLE.search(path):
        return PRIORITY_OTHER
    if _PRIVACY.search(path):
        return PRIORITY_PRIVACY
    return PRIORITY_LEGAL if _LEGAL.search(path) else PRIORITY_OTHER
