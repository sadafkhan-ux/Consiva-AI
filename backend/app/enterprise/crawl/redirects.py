"""Where a URL redirects to, found BEFORE the browser navigates to it (owner's rule, 19 Nov 2026: a redirect to a subdomain or another website is not followed for scanning).

Playwright calls a route handler only for the first URL of a navigation, not for the redirect hops after it, so a navigation guard alone cannot stop the browser from following a server redirect
to another host. Each page's redirect chain is therefore read first, hop by hop, with HEAD requests that do not follow redirects (`max_redirects=0`), every hop through the SSRF check. If any hop
leaves the submitted host, the browser never opens the page: the URL is recorded as excluded with the original address and the destination. The check is best-effort in one direction only:
when it cannot tell (HEAD refused, a network error), the page is opened as before and the final address is checked again after loading (runner._process), so an unknown never becomes a
scanned page on another host.

The requests go through the crawl's shared browser context, never the fresh context a page is inspected in, so they cannot set a cookie in the page's own pre-consent evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from app.enterprise.crawl import host
from app.scanner.url_safety import assert_safe_url

MAX_HOPS = 10
TIMEOUT_MS = 15_000


@dataclass
class RedirectCheck:
    url: str
    final_url: str | None = None                      # the last address the chain reached (None when the check could not tell)
    hops: list[dict] = field(default_factory=list)    # [{"url", "status", "location"}], first to last
    off_host: str | None = None                       # the first hop that left the submitted host, if any
    error: str | None = None                          # why the chain could not be read (the page is then opened and checked after loading)


async def check(request_context, url: str, root_url: str, *, max_hops: int = MAX_HOPS) -> RedirectCheck:
    """Follow `url`'s server redirects without opening it in a browser. Never raises."""
    out = RedirectCheck(url=url)
    current = url
    try:
        for _ in range(max_hops + 1):
            await assert_safe_url(current)
            response = await request_context.head(current, timeout=TIMEOUT_MS, max_redirects=0)
            status = response.status
            location = (response.headers or {}).get("location")
            out.hops.append({"url": current, "status": status, "location": location})
            if 300 <= status < 400 and location:
                nxt = urljoin(current, location)
                if not host.is_same_host(nxt, root_url):
                    out.off_host = nxt
                    out.final_url = nxt
                    return out
                current = nxt
                continue
            out.final_url = current
            return out
        out.error = f"more than {max_hops} redirects"
    except Exception as exc:                                     # noqa: BLE001 -- unknown, not "safe": the page is opened and its final address checked after loading
        out.error = (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)[:200]
    return out
