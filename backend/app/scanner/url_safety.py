"""SSRF guard (master prompt §5, mandatory). Resolves the hostname and rejects
private/loopback/link-local ranges (including the cloud metadata address
169.254.169.254, which falls under link-local), non-http(s) schemes, and — via
crawler.py's use of this at the browser's navigation-routing layer, not just here —
redirects into any of those. Called twice: once at scan-request time (fail fast, good
error message) and again inside the crawler before every navigation, since a
same-origin-looking URL can still redirect to an internal address at connect time.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from app.core.exceptions import ScanAuthorizationError

_ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


async def assert_safe_url(url: str) -> list[str]:
    """Async so the DNS lookup below runs on asyncio's own resolver thread pool
    (`loop.getaddrinfo`) instead of blocking the event loop -- this is called from
    request_scan (every API request) and from crawler.py's per-navigation guard (once
    per page/redirect hop during a crawl), both of which run on the shared FastAPI/
    worker event loop alongside other concurrent work.

    Returns the validated-safe IPs this hostname resolved to (all callers that only
    care about the pass/fail check can keep ignoring the return value; crawler.py's
    run_scan() uses it to pin the root hostname to one of these IPs via a Chromium
    --host-resolver-rules launch flag, closing the DNS-rebinding window between this
    check and Chromium's own separate resolution moments later -- see its call site)."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ScanAuthorizationError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ScanAuthorizationError(f"URL has no hostname: {url}")

    try:
        addr_infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ScanAuthorizationError(f"Could not resolve hostname: {parsed.hostname}") from exc

    safe_ips = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = sockaddr[0]
        if _is_blocked_ip(ip):
            raise ScanAuthorizationError(
                f"URL {url!r} resolves to a disallowed address ({ip}) — private, loopback, "
                "link-local, and other internal ranges cannot be scanned."
            )
        safe_ips.append(ip)
    return safe_ips
