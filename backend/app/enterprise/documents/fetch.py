"""Fetching a linked document safely: the same SSRF check and same-site rule as a page, redirects followed one hop at a time, a hard size limit, and nothing executed."""

from __future__ import annotations

from urllib.parse import urljoin

from playwright.async_api import BrowserContext

from app.enterprise.crawl import scope
from app.scanner.url_safety import assert_safe_url

MAX_REDIRECTS = 5


class DocumentFetchFailed(Exception):
    def __init__(self, message: str, *, retryable: bool, http_status: int | None = None) -> None:
        super().__init__(message)
        self.retryable, self.http_status = retryable, http_status


class DocumentOffHost(Exception):
    """The document redirects to a subdomain or another website: not fetched from there. `str(exc)` is the recorded exclusion reason."""

    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.http_status = http_status


class DocumentTooLarge(Exception):
    """Larger than ENTERPRISE_MAX_DOCUMENT_MB. Refused whole, never cut: a finding built on half a policy would be wrong."""


async def fetch(context: BrowserContext, url: str, root_url: str, *, max_bytes: int, timeout_ms: int = 60_000) -> tuple[bytes, str | None, str]:
    """Returns (bytes, content-type, final URL). Raises DocumentFetchFailed or DocumentTooLarge."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        await assert_safe_url(current)
        try:
            response = await context.request.get(current, timeout=timeout_ms, max_redirects=0)
        except Exception as exc:
            raise DocumentFetchFailed(str(exc).splitlines()[0][:300] or type(exc).__name__, retryable=True) from exc
        status = response.status
        if 300 <= status < 400 and (location := (response.headers or {}).get("location")):
            current = urljoin(current, location)
            if not scope.same_site(current, root_url):
                # not followed (owner's exact-host rule): the caller records the URL as excluded, with where it redirects
                raise DocumentOffHost(scope.exclusion_reason(current, root_url, context=f"{url} redirects to {current}") or "the document redirects to another host", http_status=status)
            continue
        if status >= 400:
            raise DocumentFetchFailed(f"HTTP {status}", retryable=status >= 500 or status == 429, http_status=status)
        declared = (response.headers or {}).get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise DocumentTooLarge(f"{int(declared)} bytes")
        body = await response.body()
        if len(body) > max_bytes:
            raise DocumentTooLarge(f"{len(body)} bytes")
        return body, (response.headers or {}).get("content-type"), current
    raise DocumentFetchFailed("too many redirects", retryable=False)
