"""What the Enterprise crawl may follow: pure rules, no browser and no database.

A link is one of: a PAGE to render, a DOCUMENT (PDF, DOCX: fetched and read page by page; other office formats are queued and recorded as not supported), a QUERY VARIANT (the same page with filter or
sort parameters, recorded but not crawled, so a faceted listing cannot create a million URLs), or SKIP (not a page at all: mailto:, images, scripts). Pagination parameters are
the one query a crawl follows, because the next page of a list is a different page.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urldefrag, urlparse

from app.scanner._domain import registered_domain

PAGE, DOCUMENT, QUERY_VARIANT, SKIP, EXTERNAL = "page", "document", "query_variant", "skip", "external"

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
    """On the scanned site's registered domain, over http(s) -- the same boundary the 5-page scanner uses."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and registered_domain(parsed.netloc) == registered_domain(urlparse(root_url).netloc)


def classify(url: str, root_url: str) -> str:
    parsed = urlparse(clean(url))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return SKIP
    if not same_site(url, root_url):
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


def legal_priority(url: str) -> int:
    """0 for a page a privacy assessment needs first (by its address: it names a policy, terms, grievance or contact page and is not an article), else 1. An article that mentions the law is not
    the policy, so a blog address never earns priority."""
    path = urlparse(url).path
    return 0 if _LEGAL.search(path) and not _ARTICLE.search(path) else 1
