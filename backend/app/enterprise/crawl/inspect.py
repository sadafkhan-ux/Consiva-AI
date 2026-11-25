"""Deep inspection of one page, with the 5-page agent's own first-page sequence, then the Enterprise extras (owner's requirement, 19 Nov 2026).

Every page of an Enterprise scan is inspected with the same execution sequence, depth and effort as the 5-page agent gives its first page. The 5-page helpers are IMPORTED from
app/scanner/crawler.py and called exactly as it calls them; nothing in the 5-page agent is changed. In order, on one visit of the page:

  1. the address is checked by the SSRF guard; Set-Cookie headers, failed consent-tool scripts and every request are watched from the first byte;
  2. navigation with the 5-page agent's bounded, classified retries (_navigate_with_retry), the homepage with the longer retry window the 5-page agent gives its seed page;
  3. the bounded wait for the load event and the tracker settle window (_wait_for_load_event, _TRACKER_SETTLE_MS);
  4. a bot-protection page served with HTTP 200, and a "not found" page served with 200, are recognised (_page_is_a_block_page, _page_says_it_was_not_found);
  5. the homepage screenshot, before scrolling moves the page (_capture_homepage_screenshot, homepage only, as in the 5-page agent);
  6. the progressive scroll that follows the page's own requests (_progressive_scroll);
  7. which form controls start ticked, the storage keys (names only), the consent-tool globals, the iframe signals, the settled text and control text (_FORM_CHECKED_JS, _STORAGE_KEYS_JS,
     _CMP_GLOBAL_VAR_CANDIDATES, _collect_iframe_signal_evidence, _settled_text, _settled_control_text);
  then the Enterprise extras: wait until the page is stable, open its collapsed sections (accordions, tabs, "read more"), and read its shadow DOM, same-site iframes and forms
  (page_inspect); and finally the cookie jar, the response headers and the redirect chain.

A failure to load raises renderer.RenderFailed (with the block verdict when the site's protection refused the scanner); a page that says it was not found raises NotFoundPage.
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import BrowserContext

from app.config import Settings
from app.enterprise.crawl import page_inspect
from app.enterprise.crawl.renderer import Rendered, RenderFailed, _links, _redirect_chain
from app.monitoring import blocks
from app.scanner.crawler import (
    _CMP_GLOBAL_VAR_CANDIDATES, _FORM_CHECKED_JS, _SCROLL_MAX_STEPS, _STORAGE_KEYS_JS, _TRACKER_SETTLE_MS, _capture_homepage_screenshot, _collect_iframe_signal_evidence,
    _drain_set_cookie_reads, _navigate_with_retry, _page_is_a_block_page, _page_says_it_was_not_found, _progressive_scroll, _settled_control_text, _settled_text,
    _wait_for_load_event, _watch_set_cookies,
)
from app.scanner.page_signals import indic_script_counts
from app.scanner.url_safety import assert_safe_url

logger = logging.getLogger(__name__)


class NotFoundPage(Exception):
    """The page answered 200 but says it was not found: it is not a page of the site."""


def _verdict_for_failure(http_status: int | None, exc: BaseException | None):
    """The block verdict for a navigation that failed, or None when it was not the site's protection (a timeout, a 5xx, a DNS failure)."""
    v = blocks.detect_block(http_status) if http_status else blocks.detect_block_from_error(exc)
    return v if v.blocked else None


async def deep_inspect(context: BrowserContext, url: str, *, root_url: str, settings: Settings, is_home: bool) -> Rendered:
    """Inspect one page in `context` (a fresh browser session that has given no consent). Returns what it showed; raises RenderFailed or NotFoundPage."""
    await assert_safe_url(url)
    page = await context.new_page()
    request_records: list[tuple[str, str]] = []
    page.on("request", lambda r: request_records.append((r.url, r.resource_type)) if len(request_records) < page_inspect.MAX_REQUESTS_KEPT else None)
    page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))           # an alert or confirm must never wait for a person
    set_cookie_events, set_cookie_reads = _watch_set_cookies(page, url)
    cmp_failures: list[dict] = []

    def _note_failure(request_url: str, resource_type: str, error: str) -> None:
        from app.rules.tracker_catalog import match_cmp
        if resource_type == "script" and match_cmp(script_src=request_url) is not None and not any(f["url"] == request_url for f in cmp_failures):
            cmp_failures.append({"url": request_url[:300], "error": error[:200]})

    page.on("requestfailed", lambda r: _note_failure(r.url, r.resource_type, str(r.failure or "the request failed")))
    page.on("response", lambda r: _note_failure(r.url, r.request.resource_type, f"HTTP {r.status}") if r.status >= 400 else None)
    try:
        window = settings.scanner_nav_retry_seconds_seed if is_home else settings.scanner_nav_retry_seconds_page
        response, outcome, exc, attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000, window_s=window)
        if outcome != "success":
            http_status = getattr(exc, "status", None)
            verdict = _verdict_for_failure(http_status, exc)
            message = (str(exc).splitlines()[0][:300] if exc else outcome) + f" (after {attempts} attempt(s))"
            if verdict:
                raise RenderFailed(f"{message}: {verdict.signal}", retryable=False, http_status=http_status, block=verdict)
            raise RenderFailed(message, retryable=outcome == "timeout" or (http_status or 0) >= 500, http_status=http_status)
        status = response.status if response else None
        await _wait_for_load_event(page, url)
        await page.wait_for_timeout(_TRACKER_SETTLE_MS)
        if await _page_is_a_block_page(page):
            title = await page.title()
            body = await page.evaluate("document.body ? document.body.innerText : ''")
            verdict = blocks.detect_block(status, await response.all_headers() if response else {}, title, body)
            if not verdict.blocked:
                verdict = blocks.detect_block(403, {}, title, body)             # the 5-page agent treats a bot-protection message drawn after load as a 403
            raise RenderFailed(f"the page is a bot-protection message, not the site: {verdict.signal}", retryable=False, http_status=status, block=verdict)
        if await _page_says_it_was_not_found(page):
            raise NotFoundPage("the page answered 200 but says it was not found")
        screenshot = await _capture_homepage_screenshot(page, http_status=status, final_url=page.url) if is_home else None
        scroll_steps = await _progressive_scroll(page, request_count=lambda: len(request_records))
        scroll_stopped = None
        if scroll_steps >= _SCROLL_MAX_STEPS:          # the 5-page scroll is bounded; say when it ran out before the bottom, so the page's evidence note can tell the reader
            try:
                at_bottom = await page.evaluate("() => window.innerHeight + window.scrollY >= document.body.scrollHeight - 2")
            except Exception:                                              # noqa: BLE001
                at_bottom = True                                           # unknown: never claim a limit that may not have been reached
            scroll_stopped = None if at_bottom else "max_steps"

        # The Enterprise extras, on top of the 5-page sequence: whatever goes wrong in them, the page is still read and the problem is recorded.
        expanded: dict = {}
        dom: dict = {}
        settled = None
        try:
            settled = await page_inspect.wait_until_stable(page)
            expanded = await page_inspect.expand_sections(page)
            dom = await page_inspect.collect_dom(page, root_url)
        except Exception as e:                                                   # noqa: BLE001
            expanded = expanded or {"error": f"{type(e).__name__}: {e}"[:200]}

        html = await page.content()
        try:
            checked = await page.evaluate(_FORM_CHECKED_JS)
        except Exception:                                                        # noqa: BLE001
            checked = None
        try:
            storage = await page.evaluate(_STORAGE_KEYS_JS)
        except Exception:                                                        # noqa: BLE001
            storage = None
        try:
            cmp_globals = await page.evaluate("(c) => c.filter(n => typeof window[n] !== 'undefined')", _CMP_GLOBAL_VAR_CANDIDATES)
        except Exception:                                                        # noqa: BLE001
            cmp_globals = []
        iframe_scripts, iframe_text, iframe_control_text = await _collect_iframe_signal_evidence(page)
        try:
            settled_text = await _settled_text(page, url)
            control_text = await _settled_control_text(page) if is_home else None
        except Exception:                                                        # noqa: BLE001
            settled_text, control_text = "", None
        inner = await page.evaluate("document.body ? document.body.innerText : ''")
        try:
            # The site footer (privacy contact, policy links): some sites draw it so that innerText leaves it out (consiva.ai, 20 Nov 2026: in the HTML of 94 pages, in the text of 9).
            footer = await page.evaluate("[...document.querySelectorAll('footer, [role=\"contentinfo\"]')].map(f => (f.innerText || f.textContent || '').trim()).filter(Boolean).join('\\n')")
        except Exception:                                                        # noqa: BLE001
            footer = ""
        title = await page.title()
        headers = await response.all_headers() if response else {}
        cookies = await context.cookies()
        chain = await _redirect_chain(response)
        final_url = page.url
    finally:
        await _drain_set_cookie_reads(set_cookie_reads)
        if not page.is_closed():
            try:
                await page.close()
            except Exception:                                                    # noqa: BLE001
                pass

    # The text: the settled text the 5-page agent reads (shadow DOM included) or, if it read nothing, the page's innerText; then what innerText does not reach, under a label.
    base = settled_text if len(settled_text or "") >= len(inner or "") else inner
    extra = []
    if dom.get("shadow"):
        extra.append("[shadow DOM]\n" + "\n".join(dom["shadow"]))
    for f in dom.get("frame_text", []):
        if f.get("text"):
            extra.append(f"[iframe {f['src']}]\n{f['text']}")
    footer_text = " ".join((footer or "").split())[:20000]
    if footer_text and footer_text[:120] not in " ".join((base or "").split()):
        extra.insert(0, "[footer]\n" + footer_text)
    full_text = (base or "") + ("\n\n" + "\n\n".join(extra) if extra else "")
    five_page = {
        "sequence": "5-page first-page inspection" + (" (homepage)" if is_home else ""), "navigation_attempts": attempts, "scroll_steps": scroll_steps, "settled": settled,
        "set_cookie_events": set_cookie_events[:200], "storage_keys": storage if isinstance(storage, dict) else None, "cmp_globals": sorted(set(cmp_globals or [])),
        "cmp_script_failures": cmp_failures, "form_checked_states": checked, "iframe_scripts": len(iframe_scripts), "iframe_text_chars": len(iframe_text or ""),
        "iframe_control_text": (iframe_control_text or "")[:2000], "control_text": (control_text or "")[:4000] if control_text is not None else None,
        "insecure_requests": [u for u, _ in request_records if u.lower().startswith("http://")][:10] if final_url.lower().startswith("https://") else [],
        "indic": indic_script_counts(full_text),
    }
    if screenshot is not None:
        five_page["screenshot"] = screenshot
    return Rendered(
        url=url, final_url=final_url, http_status=status, content_type=headers.get("content-type"), title=title or None, html=html, text=full_text, links=_links(html, final_url),
        headers=headers, redirect_chain=chain, requests=[{"url": u, "type": t} for u, t in request_records], cookies=cookies, dom=dom,
        scroll={"steps": scroll_steps, "settled": settled, "method": "5-page progressive scroll", **({"stopped": scroll_stopped} if scroll_stopped else {})}, expanded=expanded, five_page=five_page)
