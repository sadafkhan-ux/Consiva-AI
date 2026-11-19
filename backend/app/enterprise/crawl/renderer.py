"""Render one page in Chromium and return what it showed: the full DOM, the visible text, the title, the final address, the status and the links.

JavaScript-built pages and single-page apps are read after they settle: the page is loaded to DOMContentLoaded, given a short wait for its own load event (never an unbounded one:
a page whose third-party scripts never finish must not hold a worker), and then a fixed settle period for client-side rendering. Every navigation, including each redirect hop, is checked
by the existing SSRF guard before the request leaves.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urljoin

from playwright.async_api import BrowserContext
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.enterprise.crawl import page_inspect
from app.scanner.page_parser import parse_page
from app.scanner.url_safety import assert_safe_url

SETTLE_MS = 1200
LOAD_WAIT_MS = 5000


async def _block_verdict(page, response, status: int):
    """Is this error answer the site's bot protection or access control (as opposed to a server error)? Looks at the status, the headers and the short page that came back. Recognises only; never answers a challenge."""
    from app.monitoring import blocks
    try:
        headers = await response.all_headers()
    except Exception:
        headers = {}
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = await page.inner_text("body", timeout=2000)
    except Exception:
        body = ""
    return blocks.detect_block(status, headers, title, body)


class RenderFailed(Exception):
    """The page could not be rendered. `retryable` says whether trying again can help (a timeout or a 5xx), `http_status` carries the answer if there was one."""

    def __init__(self, message: str, *, retryable: bool, http_status: int | None = None, block=None) -> None:
        super().__init__(message)
        self.retryable, self.http_status = retryable, http_status
        self.block = block                  # a monitoring.blocks.BlockVerdict when the site's protection refused the scanner (then retrying cannot help and is not done)


@dataclass
class Rendered:
    url: str
    final_url: str
    http_status: int | None
    content_type: str | None
    title: str | None
    html: str
    text: str
    links: list[str] = field(default_factory=list)
    #: What deep inspection saw. All empty for a page that was only loaded, so a caller that ignores them behaves as before.
    headers: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)
    dom: dict = field(default_factory=dict)
    scroll: dict = field(default_factory=dict)
    expanded: dict = field(default_factory=dict)
    #: What the 5-page agent's first-page sequence observed on this page (crawl/inspect.py): Set-Cookie events, storage keys, consent-tool globals and failed consent-tool scripts,
    #: form controls that start ticked, iframe signals, insecure requests, language signals, and for the homepage its screenshot. Empty for a page read by `render` alone.
    five_page: dict = field(default_factory=dict)


def _links(html: str, base_url: str) -> list[str]:
    """Every link on the page as an absolute URL, in page order, duplicates removed. Uses the existing page parser, unchanged."""
    seen: dict[str, None] = {}
    for link in parse_page(html, base_url).links:
        if link.href:
            seen.setdefault(urljoin(base_url, link.href), None)
    return list(seen)


async def _redirect_chain(response) -> list[dict]:
    """The hops that led to the page, first to last: each address and the status it answered with."""
    hops, request = [], response.request if response else None
    while request is not None and request.redirected_from is not None:
        previous = request.redirected_from
        answer = await previous.response()
        hops.append({"url": previous.url, "status": answer.status if answer else None})
        request = previous
    return list(reversed(hops))


async def render(context: BrowserContext, url: str, *, timeout_ms: int = 30_000, settle_ms: int = SETTLE_MS, inspect: bool = True, site_url: str | None = None) -> Rendered:
    """Load one page and read it. With `inspect` (the default) it is also scrolled through, its collapsed sections are opened, and its cookies, requests, forms and response headers
    are collected. Give each page its own browser context: the cookies and requests then belong to this page alone, and the consent state is the untouched first visit."""
    await assert_safe_url(url)
    page = await context.new_page()
    requests: list[dict] = []
    if inspect:
        page.on("request", lambda r: requests.append({"url": r.url, "type": r.resource_type}) if len(requests) < page_inspect.MAX_REQUESTS_KEPT else None)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))      # an alert or confirm must never wait for a person
    try:
        try:
            response = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise RenderFailed(f"timed out after {timeout_ms} ms", retryable=True) from exc
        except Exception as exc:                      # navigation refused, DNS, TLS, connection reset
            raise RenderFailed(str(exc).splitlines()[0][:300] or type(exc).__name__, retryable=True) from exc
        status = response.status if response else None
        if status is not None and status >= 400:
            verdict = await _block_verdict(page, response, status)
            if verdict:
                raise RenderFailed(f"HTTP {status}: {verdict.signal}", retryable=False, http_status=status, block=verdict)
            raise RenderFailed(f"HTTP {status}", retryable=status >= 500, http_status=status)
        try:
            await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
        except PlaywrightTimeout:
            pass                                       # bounded wait: carry on with what has rendered
        await page.wait_for_timeout(settle_ms)

        scroll: dict = {}
        expanded: dict = {}
        dom: dict = {}
        if inspect:
            # Inspection is a bonus on top of loading: whatever goes wrong in it, the page is still read, and the problem is recorded rather than raised.
            try:
                settled = await page_inspect.wait_until_stable(page)
                scroll = await page_inspect.scroll_page(page)
                scroll["settled"] = settled
                expanded = await page_inspect.expand_sections(page)
                dom = await page_inspect.collect_dom(page, site_url or url)
            except Exception as exc:
                scroll = scroll or {"error": f"{type(exc).__name__}: {exc}"[:200]}
        html = await page.content()
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        title = await page.title()
        headers = await response.all_headers() if response else {}
        cookies = await context.cookies() if inspect else []
        chain = await _redirect_chain(response) if inspect else []
        # Text that innerText does not reach: shadow DOM and same-site iframes. Kept after the page's own text, under a label, so a reader of the evidence can tell them apart.
        extra = []
        if dom.get("shadow"):
            extra.append("[shadow DOM]\n" + "\n".join(dom["shadow"]))
        for f in dom.get("frame_text", []):
            if f.get("text"):
                extra.append(f"[iframe {f['src']}]\n{f['text']}")
        full_text = (text or "") + ("\n\n" + "\n\n".join(extra) if extra else "")
        return Rendered(
            url=url, final_url=page.url, http_status=status, content_type=headers.get("content-type"), title=title or None,
            html=html, text=full_text, links=_links(html, page.url),
            headers=headers, redirect_chain=chain, requests=requests, cookies=cookies, dom=dom, scroll=scroll, expanded=expanded)
    finally:
        await page.close()
