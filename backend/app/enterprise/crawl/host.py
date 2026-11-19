"""The Enterprise scan's host rule: ONLY the exact hostname the user submitted is scanned (owner's requirement, 19 Nov 2026). Pure functions, no browser and no database.

`example.com` submitted: `https://example.com/about` is in scope; `www.example.com`, `blog.example.com`, `example.com.evil.com` and `evil-example.com` are not. The comparison is on parsed,
normalised hostnames, never on string suffixes: the host is lowercased, a trailing dot is dropped, an internationalised name is compared in its ASCII (punycode) form, and a default port
(80 for http, 443 for https) is the same as no port. A host that is not the submitted one is either another host of the same registered domain (`excluded_subdomain`: a subdomain, the
`www.` twin, or the parent domain) or another website (`excluded_external_domain`).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.scanner._domain import registered_domain

SAME, SUBDOMAIN, EXTERNAL = "same", "subdomain", "external"
EXCLUDED_SUBDOMAIN, EXCLUDED_EXTERNAL = "excluded_subdomain", "excluded_external_domain"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def host_key(url: str) -> str | None:
    """The comparable host of `url` (`host` or `host:port` for a non-default port), or None when it is not an http(s) URL with a host."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")          # bücher.example -> xn--bcher-kva.example; an ASCII name is unchanged
    except UnicodeError:
        return None                                          # not a valid host name at all
    return host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"


def where(url: str, root_url: str) -> str | None:
    """SAME when `url` is on exactly the submitted host, SUBDOMAIN when it is another host of the same registered domain, EXTERNAL for another website, None when it is not a web URL."""
    mine, root = host_key(url), host_key(root_url)
    if mine is None or root is None:
        return None
    if mine == root:
        return SAME
    bare = mine.split(":", 1)[0]
    return SUBDOMAIN if registered_domain(bare) == registered_domain(root.split(":", 1)[0]) else EXTERNAL


def is_same_host(url: str, root_url: str) -> bool:
    return where(url, root_url) == SAME


def exclusion(url: str, root_url: str, *, context: str = "") -> str | None:
    """The recorded exclusion reason for a URL that is not on the submitted host (`excluded_subdomain: ...` / `excluded_external_domain: ...`), or None when it is on it."""
    w = where(url, root_url)
    if w in (SAME, None):
        return None
    allowed = host_key(root_url)
    label = EXCLUDED_SUBDOMAIN if w == SUBDOMAIN else EXCLUDED_EXTERNAL
    what = "another host of the submitted site" if w == SUBDOMAIN else "another website"
    return f"{label}: {what} ({host_key(url)}); only {allowed} is scanned{f' ({context})' if context else ''}"[:1000]
