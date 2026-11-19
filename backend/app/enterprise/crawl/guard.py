"""The browser-level host guard of an Enterprise scan (owner's rule, 19 Nov 2026: the browser must not navigate to a subdomain or another website as part of the scan).

Installed on every browser context the scan opens (`context.route("**/*", host_guard(root_url))`): the crawl's shared context, each page's fresh context and every consent-test session. A
TOP-LEVEL navigation (the page itself: a link, a JavaScript or meta-refresh navigation, a popup) to anything other than the exact submitted host is aborted before it leaves the browser and
recorded. Everything else goes on to the existing 5-page SSRF guard, unchanged. Embedded iframes and the page's own scripts and resources load as a normal visitor's browser would load them,
by the owner's choice: a consent banner or a tracker hosted elsewhere is the evidence (blocking them would hide it); their links are never followed or scanned.

Server redirect hops are not seen by a route handler (Playwright routes only the first URL of a navigation): they are read before navigation by crawl/redirects.py, and the final address is
checked again after loading.
"""

from __future__ import annotations

import logging

from app.enterprise.crawl import host
from app.scanner.crawler import _guard_navigation         # existing, unchanged: the SSRF re-check of every navigation

logger = logging.getLogger(__name__)


def _is_top_level(request) -> bool:
    """A navigation of a page's main frame. A request whose frame cannot be told is treated as top-level (blocked if off-host): the safe direction."""
    try:
        if not request.is_navigation_request():
            return False
    except Exception:                                        # noqa: BLE001
        return False
    try:
        return request.frame.parent_frame is None
    except Exception:                                        # noqa: BLE001
        return True


def host_guard(root_url: str, blocked: list[dict] | None = None):
    """A route handler that keeps top-level navigation on the submitted host. `blocked` (optional) collects {"url", "reason"} for each navigation it stopped, bounded."""

    async def handler(route) -> None:
        request = route.request
        if _is_top_level(request) and host.where(request.url, root_url) in (host.SUBDOMAIN, host.EXTERNAL):
            reason = host.exclusion(request.url, root_url, context="navigation blocked by the browser guard")
            logger.info("enterprise scan: blocked a navigation to %s (%s)", request.url[:200], reason)
            if blocked is not None and len(blocked) < 100:
                blocked.append({"url": request.url[:500], "reason": reason})
            try:
                await route.abort("blockedbyclient")
            except Exception:                                # noqa: BLE001 -- the page already went away
                pass
            return
        await _guard_navigation(route)

    return handler
