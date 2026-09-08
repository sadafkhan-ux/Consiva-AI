"""Playwright-driven crawler — the only module that touches a real browser / raw HTML.
Everything downstream of `run_scan()` only ever sees the structured Pydantic records
from scanner/schemas.py (docs/architecture §5: "do not send raw website HTML to the LLM").

Runs three passes per scan (master prompt §5, Build Plan Component 2):
  1. pre_consent — a full multi-page crawl, untouched by any consent interaction. This
     is the main evidence-gathering pass: pages, forms, links, policies, and every
     cookie/tracker/script observed here is tagged consent_states=["pre_consent"].
  2. post_accept — a fresh browser context, homepage only: click an Accept-like
     control (see consent_interactor.py) and observe what appears/changes.
  3. post_reject — same as (2) with a fresh context and a Reject-like control.
Passes 2-3 are scoped to the homepage rather than a full re-crawl, deliberately: the
banner's consent decision drives site-wide tag-manager configuration, so a homepage
observation is a representative signal without tripling total scan time. When no
clickable control is found, that's recorded explicitly on the consent signal's
evidence (`accept_interaction`/`reject_interaction` = "not_found"/"page_unreachable"),
never silently treated as "nothing to report."

Every one of the three passes also runs a bounded progressive scroll (see
`_progressive_scroll`) after its own settle wait, so IntersectionObserver-gated lazy
content gets a real chance to fire in every consent state, not just at initial load.
That scroll is ADAPTIVE: it keeps going while it is still surfacing network requests
and stops once it demonstrably isn't, because a re-measurement of its marginal value
per step found no site gaining a new third-party host after step 3 while a fixed
8 steps cost ~4.7s per scan. See the _SCROLL_* constants for both measurements.
"""

import asyncio
import logging
import subprocess
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import Settings, get_settings
from app.core.exceptions import ScanAuthorizationError
from app.rules.tracker_catalog import CMP_CATALOG
from app.scanner._domain import registered_domain as _registered_domain
from app.scanner.consent_interactor import click_accept, click_reject
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.cookie_detector import detect_cookies
from app.scanner.form_detector import detect_forms
from app.scanner.page_parser import ParsedScript, parse_page
from app.scanner.policy_detector import detect_policies
from app.scanner.schemas import (
    ConsentSignalRecord,
    CookieRecord,
    FormRecord,
    PageRecord,
    PolicyRecord,
    ScanResult,
    TrackerRecord,
)
from app.scanner.tracker_detector import detect_trackers
from app.scanner.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

SCANNER_VERSION = "0.2.0"
_CMP_GLOBAL_VAR_CANDIDATES = sorted({v for sig in CMP_CATALOG for v in sig.global_js_vars})

# Perf tuning (measured live against a real site, not guessed -- see the optimization
# audit): wait_until="networkidle" was costing ~900ms per navigation *past* the page's
# own real loadEventEnd (confirmed via the browser's Navigation Timing API), because it
# waits for 500ms of total network silence -- easily dragged out by ongoing analytics
# beacons/polling that never really go quiet. "load" + a short, PREDICTABLE fixed
# window still gives async-loading trackers/tag-managers a fair chance to fire (most
# real ones load within 1-2s of `load`), without the unbounded tail.
_TRACKER_SETTLE_MS = 1200

# Progressive scroll (scanner-hardening audit, live-measured against 3 real sites --
# swaransoft.com/nvidia.com/klaviyo.com -- not guessed): a load+settle-only pass misses
# IntersectionObserver-gated lazy content. Scrolling top-to-bottom surfaced 12%-65% MORE
# network requests than load+settle alone on those 3 real sites, almost entirely
# analytics/ad pixels (Google Analytics, DoubleClick, Adobe Demdex, LinkedIn Ads, The
# Trade Desk) that fire only once their container scrolls into view. Bounded on BOTH
# step count and wall-clock duration so a pathological infinite-scroll page (or one
# whose scrollHeight keeps growing) can never hang a scan.
#
# Re-measured later (marginal value per step, on prepmyevent.com / swaransoft.com /
# bbc.com/news, counting only non-static third-party requests -- i.e. excluding the
# lazy-loaded images/fonts that inflate a raw "network activity" count without carrying
# any tracking signal). Result: NO site gained a new third-party host after step 3, and
# steps 4-8 surfaced zero new hosts on all three. swaransoft.com gained nothing at all
# from any of the 8 steps. So a fixed 8 steps spent ~1.5s per pass (~4.7s per scan,
# three passes) buying nothing on typical sites.
#
# Hence _SCROLL_QUIET_STEPS: keep scrolling while it is still surfacing requests, and
# stop once it demonstrably isn't. The step/duration ceilings remain as the hard bound
# for genuinely active pages (bbc.com/news was still producing requests at step 5), so
# this trades no coverage on the sites that need scrolling -- it only stops paying for
# it on the sites that don't.
_SCROLL_MAX_STEPS = 8
_SCROLL_STEP_WAIT_MS = 300
_SCROLL_MAX_DURATION_MS = 6000
_SCROLL_SETTLE_MS = 800
_SCROLL_QUIET_STEPS = 3

# Bounded retry for a transient navigation failure (DNS blip, connection reset, TLS
# handshake timeout, a slow/timed-out load). Previously a single failed goto() had zero
# retries anywhere in the fetch path and failed the ENTIRE scan if it was the seed URL
# -- unlike the rest of this codebase, which already retries transient failures
# elsewhere (the CMP-selector search's own bounded retry, the API test harness's
# connection-retry). Applies to the seed URL, secondary crawl pages, and the homepage
# interaction passes alike; only the seed's own exhaustion is treated as fatal to the
# whole scan -- every other caller records the outcome and continues.
_NAV_MAX_ATTEMPTS = 3  # 1 initial try + 2 retries
_NAV_RETRY_BACKOFF_S = (1.0, 2.0)  # wait before retry #1, then before retry #2

# Iframe coverage for the initial consent-signal classification pass (detect_consent_
# signal). page.content() only ever returns the main frame's DOM -- confirmed by
# reading page_parser.py -- so a CMP banner whose markup lives entirely inside an
# iframe (common for cross-origin IAB TCF vendors) previously contributed zero script/
# text signal to classification, even though consent_interactor.py could already CLICK
# it (it searches page.frames directly, independent of this). Bounded smaller than
# consent_interactor's own 20-frame click-search cap: this runs once per crawled page
# across up to scanner_max_pages pages, not just the two homepage interaction passes.
_MAX_IFRAMES_FOR_SIGNAL_DETECTION = 5

# Shadow-DOM-aware text collection for consent-signal keyword detection. Real, live
# finding (scanner-hardening audit): klaviyo.com's actual cookie banner (a real
# vendor, Transcend) renders inside an OPEN shadow root -- consent_interactor.py's
# Playwright locators already pierce shadow roots and could click "Accept All" just
# fine, but detect_consent_signal's keyword search (built from page_parser.py's
# BeautifulSoup parse of page.content()) could NOT see that text at all: shadow root
# content is never included in page.content()'s serialized HTML, regardless of any
# length cap. document.body.innerText ALSO does not pierce shadow roots by default --
# confirmed live -- so this walks every open shadow root explicitly (bounded depth,
# same rationale as _MAX_IFRAMES_FOR_SIGNAL_DETECTION: this runs once per crawled
# page). Without this, a shadow-DOM-hosted banner is invisible to classification even
# though it is fully clickable -- a real, confirmed correctness gap, not theoretical.
_SHADOW_DOM_TEXT_JS = """() => {
    function collect(root, depth, out) {
        if (depth > 6) return;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) {
                out.push(el.shadowRoot.textContent || '');
                collect(el.shadowRoot, depth + 1, out);
            }
        }
    }
    const out = [document.body.innerText || ''];
    collect(document, 0, out);
    return out.join(' ');
}"""


async def _guard_navigation(route: Route) -> None:
    """SSRF defense-in-depth (docs: url_safety.py): re-validates every navigation,
    including each hop of a redirect chain, at connect time — not just the URL the
    caller originally submitted. Subresource requests (tracker scripts, images) are
    deliberately left untouched; blocking those would defeat the scanner's purpose."""
    request = route.request
    if request.is_navigation_request():
        try:
            await assert_safe_url(request.url)
        except ScanAuthorizationError:
            logger.warning("Blocked navigation to disallowed URL: %s", request.url)
            await route.abort()
            return
    await route.continue_()


async def _fetch_robots_disallow(context: BrowserContext, root_url: str) -> set[str]:
    """Best-effort robots.txt fetch for the '*' user-agent group. Not a full RFC-9309
    parser (no wildcard/pattern matching, no per-agent groups beyond '*') — enough to
    honor an explicit "don't crawl this path" without pulling in a new dependency."""
    parsed = urlparse(root_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    disallowed: set[str] = set()
    try:
        # `context.request` is a standalone API client, not the page-level network
        # stack -- it is NOT intercepted by `_guard_navigation` above, and follows
        # redirects by default. A hostile site's own robots.txt could otherwise 30x
        # this backend-issued request to an internal/metadata address (SSRF via
        # redirect). max_redirects=0 closes that: a redirect response is returned
        # as-is (fails the `.ok` check below and is skipped) rather than followed.
        response = await context.request.get(robots_url, timeout=5000, max_redirects=0)
        if response.ok:
            applies = False
            for line in (await response.text()).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("user-agent:"):
                    applies = line.split(":", 1)[1].strip() == "*"
                elif applies and line.lower().startswith("disallow:"):
                    path = line.split(":", 1)[1].strip()
                    if path:
                        disallowed.add(path)
    except Exception as exc:  # noqa: BLE001 — robots.txt is best-effort, never fatal
        logger.info("Could not fetch robots.txt for %s: %s", root_url, exc)
    return disallowed


def _is_robots_disallowed(url: str, disallowed_prefixes: set[str]) -> bool:
    path = urlparse(url).path or "/"
    return any(path.startswith(prefix) for prefix in disallowed_prefixes)


class _SeedUnreachable(Exception):
    """Internal signal that the seed/root URL itself failed to load -- distinct from a
    secondary page failure, which is fine to skip. Raised from inside a concurrent
    gather() batch and re-raised (as the original RuntimeError) by the caller once
    outside the batch, so a sibling task's own exception can't shadow it."""

    def __init__(self, url: str, cause: Exception | None, attempts: int = 1):
        self.url = url
        self.cause = cause
        self.attempts = attempts


async def _navigate_with_retry(browser_page: Page, url: str, timeout_ms: int) -> tuple[object | None, str, Exception | None, int]:
    """Attempts browser_page.goto() up to _NAV_MAX_ATTEMPTS times with backoff, for ANY
    transient failure (network error or timeout) -- never retries forever, and never
    hides the original error: it's returned (not swallowed) so the caller can decide
    what a permanent failure means (fail the whole scan for the seed, or record+skip
    for anything else) and can log/persist the real cause, not a generic message.

    Returns (response, status, exception, attempts_used). status is "success",
    "failed" (any non-timeout navigation error), or "timeout" (Playwright's own
    TimeoutError specifically) -- distinguished so callers can record which."""
    last_exc: Exception | None = None
    status = "failed"
    for attempt in range(_NAV_MAX_ATTEMPTS):
        try:
            response = await browser_page.goto(url, timeout=timeout_ms, wait_until="load")
            return response, "success", None, attempt + 1
        except PlaywrightTimeoutError as exc:
            last_exc = exc
            status = "timeout"
        except Exception as exc:  # noqa: BLE001 -- any other navigation failure is equally retryable
            last_exc = exc
            status = "failed"
        if attempt < _NAV_MAX_ATTEMPTS - 1:
            logger.warning(
                "Navigation attempt %d/%d failed for %s (%s): %s -- retrying in %.0fs",
                attempt + 1, _NAV_MAX_ATTEMPTS, url, status, last_exc, _NAV_RETRY_BACKOFF_S[attempt],
            )
            await asyncio.sleep(_NAV_RETRY_BACKOFF_S[attempt])
    return None, status, last_exc, _NAV_MAX_ATTEMPTS


async def _progressive_scroll(page: Page, request_count: Callable[[], int] | None = None) -> int:
    """Bounded, stepped scroll toward the bottom so IntersectionObserver-gated lazy
    content (ad/analytics pixels, infinite-scroll sections) gets a real chance to fire
    -- see the _SCROLL_* constants' docstring above for the live measurements that
    justified this and then bounded it.

    Stops on the FIRST of four conditions: the page reports it is at the bottom;
    `_SCROLL_QUIET_STEPS` consecutive steps produce no new network requests (the
    adaptive exit -- most sites stop surfacing anything after ~3 steps, so continuing
    is pure latency); `_SCROLL_MAX_STEPS`; or `_SCROLL_MAX_DURATION_MS`. The last two
    remain hard bounds so genuine infinite scroll can never hang a scan.

    `request_count` is a zero-arg callable returning the number of network requests
    seen so far on this page (the caller already accumulates these for evidence, so
    this reuses that list rather than attaching a second listener). When it is None the
    adaptive exit is simply disabled and behavior falls back to the step/duration
    bounds. Returns the number of scroll steps actually taken (0 if the page couldn't
    be scrolled at all -- recorded for diagnostics, never fatal to the scan)."""
    start = time.monotonic()
    steps_taken = 0
    quiet_steps = 0
    for _ in range(_SCROLL_MAX_STEPS):
        if (time.monotonic() - start) * 1000 > _SCROLL_MAX_DURATION_MS:
            break
        before = request_count() if request_count else None
        try:
            reached_bottom = await page.evaluate(
                """() => {
                    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 2;
                    if (!atBottom) window.scrollBy(0, window.innerHeight);
                    return atBottom;
                }"""
            )
        except PlaywrightError as exc:
            logger.debug("Scroll step failed (page likely mid-navigation or closed): %s", exc)
            break
        steps_taken += 1
        if reached_bottom:
            break
        await page.wait_for_timeout(_SCROLL_STEP_WAIT_MS)
        if before is not None:
            # Counted AFTER the step wait, so a request triggered by this step has had
            # the same window to arrive as it would have had before this change.
            quiet_steps = 0 if request_count() > before else quiet_steps + 1
            if quiet_steps >= _SCROLL_QUIET_STEPS:
                logger.debug(
                    "Scroll stopped early after %d steps -- %d consecutive steps surfaced no new requests",
                    steps_taken, quiet_steps,
                )
                break
    try:
        await page.wait_for_timeout(_SCROLL_SETTLE_MS)
    except PlaywrightError:
        pass
    return steps_taken


async def _collect_iframe_signal_evidence(page: Page) -> tuple[list[ParsedScript], str]:
    """Extends detect_consent_signal's inputs to also see same-page iframe content --
    page.content() (the crawler's only HTML source otherwise) returns ONLY the main
    frame's DOM, so a CMP banner whose markup lives entirely inside an iframe
    previously contributed zero script/text signal to classification. Bounded to
    _MAX_IFRAMES_FOR_SIGNAL_DETECTION child frames; best-effort -- a frame that can't
    be read (detached mid-read, cross-origin content restrictions) is skipped, never
    fatal to the page fetch."""
    scripts: list[ParsedScript] = []
    text_parts: list[str] = []
    child_frames = [f for f in page.frames if f != page.main_frame][:_MAX_IFRAMES_FOR_SIGNAL_DETECTION]
    for frame in child_frames:
        try:
            html = await frame.content()
        except PlaywrightError as exc:
            logger.debug("Could not read iframe content at %r: %s", frame.url, exc)
            continue
        parsed = parse_page(html, frame.url or page.url)
        scripts.extend(parsed.scripts)
        text_parts.append(parsed.visible_text)
    return scripts, " ".join(text_parts)


async def _fetch_one_page(context: BrowserContext, url: str, root_url: str, settings: Settings) -> dict:
    """Fetches and parses exactly one page. ALWAYS returns a dict describing the
    outcome -- a secondary page that fails to load (after bounded retries) is recorded
    explicitly with status="failed"/"timeout", never silently dropped, so the scan's
    page list always reflects every URL actually attempted. Raises _SeedUnreachable
    only when the SEED url itself is unreachable after retries -- that must fail the
    whole scan loudly, not look like a clean empty site."""
    is_seed = url == root_url
    # (url, resource_type) pairs, not just urls -- resource_type (Playwright's own
    # classification: "script"/"xhr"/"fetch"/"image"/"font"/"stylesheet"/"media"/...)
    # lets tracker_detector.py tell a real tracking script/beacon apart from a plain
    # static asset (a product photo, a font file) with no behavioral-tracking signal.
    request_records: list[tuple[str, str]] = []
    browser_page = await context.new_page()
    browser_page.on(
        "request", lambda req, _r=request_records: _r.append((req.url, req.resource_type))
    )

    try:
        response, status, exc, attempts = await _navigate_with_retry(
            browser_page, url, settings.scanner_timeout_seconds * 1000
        )
        if status != "success":
            logger.warning("Failed to load %s after %d attempt(s) (%s): %s", url, attempts, status, exc)
            if is_seed:
                raise _SeedUnreachable(url, exc, attempts=attempts)
            return {"url": url, "status": status, "attempts": attempts, "error": str(exc)}

        # Bounded settle window (see _TRACKER_SETTLE_MS) replacing "networkidle" --
        # gives async-loading trackers/tag-managers a fair, predictable chance to fire.
        await browser_page.wait_for_timeout(_TRACKER_SETTLE_MS)
        scroll_steps = await _progressive_scroll(browser_page, request_count=lambda: len(request_records))
        http_status = response.status if response else None
        html = await browser_page.content()
        global_vars_present = await browser_page.evaluate(
            """(candidates) => candidates.filter(name => typeof window[name] !== 'undefined')""",
            _CMP_GLOBAL_VAR_CANDIDATES,
        )
        iframe_scripts, iframe_text = await _collect_iframe_signal_evidence(browser_page)
        try:
            rendered_text = await browser_page.evaluate(_SHADOW_DOM_TEXT_JS)
        except PlaywrightError as exc:
            logger.debug("Could not collect shadow-DOM text for %s: %s", url, exc)
            rendered_text = ""
    except _SeedUnreachable:
        raise
    except Exception as exc:  # page loaded (or retries exhausted) but post-load processing failed
        logger.warning("Failed processing %s: %s", url, exc)
        if is_seed:
            raise _SeedUnreachable(url, exc, attempts=1) from exc
        return {"url": url, "status": "failed", "attempts": 1, "error": str(exc)}
    finally:
        if not browser_page.is_closed():
            await browser_page.close()

    parsed = parse_page(html, url)
    return {
        "url": url, "status": "success", "attempts": attempts, "http_status": http_status, "title": parsed.title,
        "request_records": request_records, "parsed": parsed, "global_vars": set(global_vars_present),
        "scroll_steps": scroll_steps, "iframe_scripts": iframe_scripts, "iframe_text": iframe_text,
        "rendered_text": rendered_text,
    }


async def _crawl_full_site(
    context: BrowserContext, root_url: str, settings: Settings, disallowed_prefixes: set[str]
) -> tuple[list[PageRecord], list[FormRecord], list[CookieRecord], list[TrackerRecord],
           list[PolicyRecord], ConsentSignalRecord, dict]:
    """Pass 1 (pre_consent): the full multi-page crawl and main evidence-gathering pass.
    Fetches up to settings.scanner_max_concurrent_pages pages at once (bounded, not
    unbounded -- matches the BFS queue's own natural batching), rather than one page at
    a time. Also returns a diagnostics dict (pages_failed/pages_timeout/scroll steps)
    for scan-metadata reporting -- never silently dropped, never fabricated."""
    site_domain = urlparse(root_url).netloc
    site_registered = _registered_domain(site_domain)

    pages: list[PageRecord] = []
    forms: list[FormRecord] = []
    scripts_by_page = {}
    network_requests_by_page = {}
    all_links = []
    crawled_page_text = {}
    all_visible_text_parts = []
    all_scripts_flat = []
    detected_global_vars: set[str] = set()
    diagnostics = {"pages_failed": 0, "pages_timeout": 0, "pages_blocked_robots": 0, "total_scroll_steps": 0}

    queue = deque([root_url])
    seen_urls = {root_url}
    page_index = 0
    max_concurrent = settings.scanner_max_concurrent_pages

    while queue and page_index < settings.scanner_max_pages:
        # Build one round: drain robots-disallowed URLs immediately (cheap, synchronous,
        # no reason to occupy a concurrency slot), collect the rest into a bounded batch.
        batch: list[str] = []
        while queue and len(batch) < max_concurrent and page_index + len(batch) < settings.scanner_max_pages:
            url = queue.popleft()
            if _is_robots_disallowed(url, disallowed_prefixes):
                logger.info("Skipping %s (robots.txt disallow)", url)
                pages.append(PageRecord(
                    local_id=f"page-{page_index}", url=url, title=None,
                    http_status=None, discovered_via="robots_disallowed",
                ))
                diagnostics["pages_blocked_robots"] += 1
                page_index += 1
                continue
            batch.append(url)

        if not batch:
            continue

        try:
            results = await asyncio.gather(*(_fetch_one_page(context, url, root_url, settings) for url in batch))
        except _SeedUnreachable as exc:
            # A secondary page's own failure inside the same gather() is recorded as an
            # explicit failed/timeout PageRecord (never raised) rather than propagating
            # here, so this can only be the seed URL -- safe to fail the whole scan.
            raise RuntimeError(
                f"Could not reach the site at all: {exc.url} after {exc.attempts} attempt(s) ({exc.cause})"
            ) from exc.cause

        for url, result in zip(batch, results, strict=True):
            page_local_id = f"page-{page_index}"
            page_index += 1

            if result["status"] != "success":
                # Never silently skip: a secondary page that failed after bounded
                # retries still gets an explicit PageRecord, distinguishing "timeout"
                # from any other "failed" -- never left out of the pages list.
                pages.append(PageRecord(
                    local_id=page_local_id, url=url, title=None, http_status=None,
                    discovered_via=result["status"],  # "failed" | "timeout"
                ))
                diagnostics["pages_timeout" if result["status"] == "timeout" else "pages_failed"] += 1
                continue

            parsed = result["parsed"]
            detected_global_vars.update(result["global_vars"])
            pages.append(PageRecord(
                local_id=page_local_id, url=url, title=result["title"], http_status=result["http_status"],
                discovered_via="seed" if url == root_url else "link",
            ))
            forms.extend(detect_forms(page_local_id, parsed.forms))
            scripts_by_page[page_local_id] = parsed.scripts + result["iframe_scripts"]
            network_requests_by_page[page_local_id] = result["request_records"]
            all_links.extend(parsed.links)
            crawled_page_text[url] = parsed.visible_text
            # rendered_text (document.body.innerText + every open shadow root's text,
            # see _SHADOW_DOM_TEXT_JS) is a strict superset of parsed.visible_text for
            # THIS purpose -- used only for consent-signal keyword detection, not for
            # crawled_page_text/policy_detector above, which is unaffected.
            all_visible_text_parts.append(result["rendered_text"] or parsed.visible_text)
            if result["iframe_text"]:
                all_visible_text_parts.append(result["iframe_text"])
            all_scripts_flat.extend(parsed.scripts)
            all_scripts_flat.extend(result["iframe_scripts"])
            diagnostics["total_scroll_steps"] += result["scroll_steps"]

            for link in parsed.links:
                if (
                    link.href not in seen_urls
                    and _registered_domain(urlparse(link.href).netloc) == site_registered
                ):
                    seen_urls.add(link.href)
                    queue.append(link.href)

    raw_cookies = await context.cookies()
    cookies = detect_cookies(raw_cookies, site_domain)
    trackers = detect_trackers(
        site_domain=site_domain, scripts_by_page=scripts_by_page,
        network_requests_by_page=network_requests_by_page,
    )
    policies = detect_policies(all_links, crawled_page_text)
    consent_signal = detect_consent_signal(
        scripts=all_scripts_flat, visible_text=" ".join(all_visible_text_parts),
        detected_global_vars=sorted(detected_global_vars),
    )

    cookies = [c.model_copy(update={"consent_states": ["pre_consent"]}) for c in cookies]
    trackers = [t.model_copy(update={"consent_states": ["pre_consent"]}) for t in trackers]

    return pages, forms, cookies, trackers, policies, consent_signal, diagnostics


async def _crawl_single_page_with_interaction(
    context: BrowserContext,
    root_url: str,
    settings: Settings,
    disallowed_prefixes: set[str],
    consent_state: str,
    click_fn: Callable[[Page], Awaitable[str]],
) -> tuple[list[CookieRecord], list[TrackerRecord], str, int]:
    """Pass 2/3 (post_accept / post_reject): homepage only, in a fresh context --
    establish the state (click), collect baseline evidence (settle), scroll (bounded),
    then capture evidence again so trackers that only fire post-scroll are attributed
    to this consent state too, not missed entirely.

    Returns (cookies, trackers, interaction_status, scroll_steps). interaction_status
    is one of the RAW outcomes below -- run_scan() composes these (together with the
    pre_consent consent_signal's mechanism_type) into the final, more specific
    cmp_not_found/cmp_not_automatable distinction surfaced on the ScanResult:
      "clicked"          -- a real Accept/Reject-like control was found and clicked
      "click_failed"      -- a control was found, but clicking it raised (detached,
                            covered by an overlay) -- distinct from "not_found"
      "not_found"        -- the page loaded fine but no clickable control was found
      "page_unreachable" -- the homepage itself never loaded (robots-disallowed, nav
                            failure after retries) -- distinct from "not_found" so a
                            caller can't mistake "we never even looked" for "we looked
                            and there's genuinely no banner here"."""
    if _is_robots_disallowed(root_url, disallowed_prefixes):
        return [], [], "page_unreachable", 0

    site_domain = urlparse(root_url).netloc
    request_records: list[tuple[str, str]] = []
    browser_page = await context.new_page()
    browser_page.on(
        "request", lambda req, _r=request_records: _r.append((req.url, req.resource_type))
    )

    scripts: list = []
    interaction_status = "page_unreachable"
    scroll_steps = 0
    try:
        _, nav_status, exc, attempts = await _navigate_with_retry(
            browser_page, root_url, settings.scanner_timeout_seconds * 1000
        )
        if nav_status != "success":
            logger.warning(
                "Could not load %s for %s interaction pass after %d attempt(s): %s",
                root_url, consent_state, attempts, exc,
            )
        else:
            await browser_page.wait_for_timeout(_TRACKER_SETTLE_MS)  # see _TRACKER_SETTLE_MS docstring above
            interaction_status = await click_fn(browser_page)
            if interaction_status == "clicked":
                await browser_page.wait_for_timeout(2000)  # let post-click network activity settle -- separate from
                # _TRACKER_SETTLE_MS above: this one is conditional on an actual consent decision having just
                # fired, giving *that* specific action's downstream tag-manager effects time to propagate
            scroll_steps = await _progressive_scroll(browser_page, request_count=lambda: len(request_records))
            html = await browser_page.content()
            scripts = parse_page(html, root_url).scripts
    except Exception as exc:  # noqa: BLE001 — this pass is best-effort; failure is recorded, not fatal
        logger.warning("Failed %s interaction pass for %s: %s", consent_state, root_url, exc)
    finally:
        if not browser_page.is_closed():
            await browser_page.close()

    raw_cookies = await context.cookies()
    cookies = detect_cookies(raw_cookies, site_domain)
    trackers = detect_trackers(
        site_domain=site_domain,
        scripts_by_page={"page-0": scripts},
        network_requests_by_page={"page-0": request_records},
    )

    cookies = [c.model_copy(update={"consent_states": [consent_state]}) for c in cookies]
    trackers = [t.model_copy(update={"consent_states": [consent_state]}) for t in trackers]
    return cookies, trackers, interaction_status, scroll_steps


def _merge_cookie_pass(base: list[CookieRecord], new: list[CookieRecord]) -> list[CookieRecord]:
    by_key = {(c.name, c.domain): i for i, c in enumerate(base)}
    for cookie in new:
        key = (cookie.name, cookie.domain)
        if key in by_key:
            idx = by_key[key]
            existing = base[idx]
            merged = existing.consent_states + [s for s in cookie.consent_states if s not in existing.consent_states]
            base[idx] = existing.model_copy(update={"consent_states": merged})
        else:
            record = cookie.model_copy(update={"local_id": f"cookie-{len(base)}"})
            base.append(record)
            by_key[key] = len(base) - 1
    return base


def _merge_tracker_pass(base: list[TrackerRecord], new: list[TrackerRecord]) -> list[TrackerRecord]:
    # Keyed on the URL without its query string, matching tracker_detector.py's dedup
    # key -- a beacon whose cache-busting params differ between the pre_consent pass
    # and an interaction pass is the SAME tracker and must merge into one record with
    # combined consent_states, not appear as two separately-tagged trackers.
    def _key(t: TrackerRecord) -> str:
        return t.script_src.split("?", 1)[0]

    by_key = {_key(t): i for i, t in enumerate(base)}
    for tracker in new:
        key = _key(tracker)
        if key in by_key:
            idx = by_key[key]
            existing = base[idx]
            merged = existing.consent_states + [s for s in tracker.consent_states if s not in existing.consent_states]
            base[idx] = existing.model_copy(update={"consent_states": merged})
        else:
            record = tracker.model_copy(update={"local_id": f"tracker-{len(base)}"})
            base.append(record)
            by_key[key] = len(base) - 1
    return base


def _host_resolver_pin_args(hostname: str, safe_ips: list[str]) -> list[str]:
    """Closes the DNS-rebinding TOCTOU between assert_safe_url's own resolution and
    Chromium's separate one moments later: forces Chromium's resolver to use the
    SAME IP this process already validated, for this hostname, for the life of the
    browser -- an attacker changing the DNS answer after this point can no longer
    change what Chromium actually connects to. Prefers an IPv4 address (simpler
    --host-resolver-rules MAP syntax); falls back to the first result otherwise.
    Only pins the root/seed hostname -- a same-registered-domain link to a genuinely
    different subdomain discovered mid-crawl still relies on _guard_navigation's
    per-navigation re-check alone (a known, documented residual gap)."""
    if not safe_ips:
        return []
    ipv4 = next((ip for ip in safe_ips if ":" not in ip), None)
    pin_target = ipv4 or f"[{safe_ips[0]}]"
    return [f"--host-resolver-rules=MAP {hostname} {pin_target}"]


def _new_context_kwargs(settings: Settings) -> dict:
    """Shared context options for every browser.new_context() call site. locale +
    Accept-Language default to India (en-IN) -- this product's DPDP compliance
    audience -- per the scanner-hardening audit's section 8: this is a request-header/
    navigator hint ONLY. It does NOT change network egress IP, and it does NOT make a
    geo-IP-gated CMP show a banner it wouldn't otherwise show to this server's actual
    vantage point (confirmed live this same audit: klaviyo.com rendered zero OneTrust
    banner elements from this server's IP, entirely independent of any browser-side
    locale/header setting) -- that limitation needs a real India-region proxy/egress,
    a separate infrastructure decision this change does not make."""
    return {
        "user_agent": settings.scanner_user_agent,
        "locale": settings.scanner_locale,
        "extra_http_headers": {"Accept-Language": settings.scanner_accept_language},
    }


def _final_interaction_status(raw_status: str, mechanism_type: str) -> str:
    """Composes the raw click-attempt outcome with the independently-detected
    consent_signal.mechanism_type (from the pre_consent pass) into the more specific
    vocabulary the scanner-hardening audit's section 8 asks for -- reusing existing
    data, no new detection logic: a "not_found" click result means something
    different depending on whether a consent mechanism was ever seen on this site at
    all ("cmp_not_found") versus one WAS seen but couldn't be automated
    ("cmp_not_automatable", e.g. a custom/bespoke banner with no catalogued selector
    and no matching generic keyword). "clicked"/"click_failed"/"page_unreachable"
    pass through unchanged -- they're already unambiguous on their own."""
    if raw_status != "not_found":
        return raw_status
    return "cmp_not_found" if mechanism_type == "none" else "cmp_not_automatable"


async def _run_interaction_pass(
    browser, root_url: str, settings: Settings, disallowed_prefixes: set[str],
    consent_state: str, click_fn: Callable[[Page], Awaitable[str]],
) -> tuple[list[CookieRecord], list[TrackerRecord], str, int]:
    """Owns one interaction pass's whole context lifecycle (create, route-guard,
    close) so two of these can run concurrently via asyncio.gather() in run_scan()
    without sharing any state -- each gets its own fresh browser context, same as
    the previous sequential version did. This isolation is deliberately preserved,
    not optimized away: the three consent states must never leak cookies/storage/
    consent choices into each other."""
    context = await browser.new_context(**_new_context_kwargs(settings))
    await context.route("**/*", _guard_navigation)
    try:
        return await _crawl_single_page_with_interaction(
            context, root_url, settings, disallowed_prefixes, consent_state, click_fn
        )
    finally:
        await context.close()


async def run_scan(root_url: str, settings: Settings | None = None) -> ScanResult:
    settings = settings or get_settings()
    safe_ips = await assert_safe_url(root_url)  # fail fast; _guard_navigation re-checks every hop below
    launch_args = _host_resolver_pin_args(urlparse(root_url).hostname, safe_ips)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.scanner_headless, args=launch_args)
        try:
            context = await browser.new_context(**_new_context_kwargs(settings))
            await context.route("**/*", _guard_navigation)
            disallowed_prefixes = await _fetch_robots_disallow(context, root_url)
            try:
                pages, forms, cookies, trackers, policies, consent_signal, pre_consent_diagnostics = await _crawl_full_site(
                    context, root_url, settings, disallowed_prefixes
                )
            finally:
                await context.close()

            # post_accept and post_reject are fully independent -- separate contexts
            # (deliberately fresh, so neither pass's consent state leaks into the
            # other), no shared mutable state between them. Running them concurrently
            # instead of sequentially is a pure wall-clock win with no correctness
            # change: same navigations, same click attempts, same detection logic.
            (
                (accept_cookies, accept_trackers, accept_status, accept_scroll_steps),
                (reject_cookies, reject_trackers, reject_status, reject_scroll_steps),
            ) = await asyncio.gather(
                _run_interaction_pass(browser, root_url, settings, disallowed_prefixes, "post_accept", click_accept),
                _run_interaction_pass(browser, root_url, settings, disallowed_prefixes, "post_reject", click_reject),
            )
        finally:
            await browser.close()

    cookies = _merge_cookie_pass(cookies, accept_cookies)
    cookies = _merge_cookie_pass(cookies, reject_cookies)
    trackers = _merge_tracker_pass(trackers, accept_trackers)
    trackers = _merge_tracker_pass(trackers, reject_trackers)

    consent_signal.evidence = {
        **consent_signal.evidence,
        "accept_interaction": _final_interaction_status(accept_status, consent_signal.mechanism_type),
        "reject_interaction": _final_interaction_status(reject_status, consent_signal.mechanism_type),
    }

    scan_diagnostics = {
        **pre_consent_diagnostics,
        "accept_scroll_steps": accept_scroll_steps,
        "reject_scroll_steps": reject_scroll_steps,
    }

    return ScanResult(
        domain=urlparse(root_url).netloc,
        root_url=root_url,
        scanner_version=SCANNER_VERSION,
        pages=pages,
        forms=forms,
        cookies=cookies,
        trackers=trackers,
        third_party_services=[],  # populated by rules.consent_rules.derive_third_party_services(), not the scanner
        policies=policies,
        consent_signals=consent_signal,
        scan_diagnostics=scan_diagnostics,
    )


async def run_scan_isolated(root_url: str, settings: Settings | None = None) -> ScanResult:
    """The entrypoint services should call instead of `run_scan()` directly.

    On Windows, spawns `run_single_scan.py` as a separate process so Playwright gets
    its own fresh (Proactor) event loop, decoupled from the parent process's
    SelectorEventLoop policy (set for the LangGraph checkpointer's psycopg — see
    app/jobs/worker.py and app/main.py). Uses plain `subprocess.run` in a thread
    executor, not `asyncio.create_subprocess_exec`: asyncio subprocess creation is
    itself unavailable under SelectorEventLoop on Windows, so the parent can't use
    asyncio's own subprocess APIs to launch the child either.

    On other platforms this conflict doesn't exist, so it just calls `run_scan()`
    directly — no subprocess overhead.
    """
    if sys.platform != "win32":
        return await run_scan(root_url, settings)

    backend_dir = Path(__file__).resolve().parent.parent.parent

    def _run_subprocess() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "app.scanner.run_single_scan", root_url],
            cwd=str(backend_dir),
            capture_output=True,
            text=True,
            # Explicit UTF-8 on both ends of this pipe -- text=True alone decodes
            # using locale.getpreferredencoding(False), which on Windows is the
            # console's ANSI codepage (e.g. cp1252), not UTF-8. run_single_scan.py's
            # own stdout/stderr are reconfigured to UTF-8 to match; without both
            # sides agreeing, real scanned website content containing ordinary
            # Unicode (arrows, dashes, non-Latin scripts) crashes the whole scan --
            # confirmed live against prepmyevent.com before this fix.
            encoding="utf-8",
            check=False,
            timeout=(settings.scanner_max_pages if settings else 25) * (settings.scanner_timeout_seconds if settings else 30) + 120,
        )

    loop = asyncio.get_running_loop()
    completed = await loop.run_in_executor(None, _run_subprocess)

    if completed.returncode != 0:
        raise RuntimeError(f"Isolated scan process failed for {root_url}: {completed.stderr.strip()}")

    return ScanResult.model_validate_json(completed.stdout)
