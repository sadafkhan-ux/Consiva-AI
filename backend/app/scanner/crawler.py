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
import base64
import heapq
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import Settings, get_settings
from app.core.exceptions import ScanAuthorizationError, ScanTimeoutError
from app.rules.tracker_catalog import CMP_CATALOG, match_cmp
from app.scanner._domain import registered_domain as _registered_domain
from app.scanner.consent_interactor import (
    click_accept,
    click_reject,
    confirm_interaction_completed,
    pass_consent_gate,
)
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.cookie_detector import (
    attribute_new_cookies,
    cookie_key,
    detect_cookies,
    parse_set_cookie_headers,
)
from app.scanner.crawl_controls import DomainRateLimiter, ResourceGuard
from app.scanner.failures import (
    DNS,
    PERMANENT,
    REFUSED,
    NavigationHttpError,
    RobotsUnreadable,
    ScanNotPossible,
    certificate_failure,
    classify_navigation_failure,
    explain_seed_failure,
    first_line,
    is_certificate_error,
    parse_retry_after,
)
from app.scanner.form_detector import detect_forms
from app.scanner.image_blocking import ImageSkipper
from app.scanner.page_parser import ParsedScript, parse_page
from app.scanner.page_signals import (
    banner_on_page,
    child_directed_signal,
    indic_script_counts,
    links_to_privacy_notice,
    merge_child_surface,
    merge_language,
    merge_mixed_content,
    merge_notice_links,
    merge_security,
    merge_storage_keys,
    response_headers,
)
from app.scanner.policy_detector import detect_policies
from app.scanner.soft_404 import looks_not_found
from app.scanner.purposes import run_purpose_test
from app.scanner.reject_and_compare import run_button_comparison, run_reject_persistence
from app.scanner.tls_assessment import assess_site_tls
from app.scanner.robots import RobotsRules, parse_robots
from app.scanner.scan_profiles import APPLICATION, MAX_WEB_PAGES, ScanProfile, resolve_profile
from app.scanner.schemas import (
    ConsentSignalRecord,
    CookieRecord,
    FormRecord,
    PageRecord,
    PolicyRecord,
    ScanResult,
    ScreenshotRecord,
    TrackerRecord,
)
from app.scanner.sitemap import discover_sitemap_urls
from app.scanner.tls_diagnostics import describe_certificate
from app.scanner.tls_diagnostics import sentence as tls_sentence
from app.scanner.tracker_detector import detect_trackers
from app.scanner.url_inventory import (
    SITEMAP_DEPTH,
    UNCONTROLLED_CAP,
    InventoryTracker,
    incomplete_extraction,
)
from app.scanner.url_normalize import (
    category_of,
    effective_rank,
    is_policy_page,
    normalize_url,
    path_without_query,
    query_variant_of,
    section_of,
    tie_break_key,
)
from app.scanner.url_safety import assert_safe_url
from app.scanner.withdrawal import run_withdrawal_test

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

#: How many skipped pages are listed individually in a scan's diagnostics (the per-reason counts are always complete).
_MAX_SKIPPED_PAGES_LISTED = 50

#: How many other hosts (subdomains and external sites) a scan lists individually.


_FRAMESET_RE = re.compile(r"<frameset\b", re.IGNORECASE)
_FRAME_SRC_RE = re.compile(r"<frame\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)   # <frame>, not <iframe>


def _frameset_sources(html: str, base_url: str) -> list[str]:
    """Absolute URLs of the <frame>s of a frameset document, or [] for any other page.

    A page that is only a <frameset> has no content of its own: the browser shows whatever its frames load, often another
    site. smartians.ai is one (a single frame onto corp.gignaati.com). Ordinary <iframe> embeds are not looked at --
    they are widgets on a real page, not the page."""
    if not _FRAMESET_RE.search(html or ""):
        return []
    return [urljoin(base_url, src.strip()) for src in _FRAME_SRC_RE.findall(html)]


def _host_key(url: str) -> str:
    """A page's host for the main-domain-vs-subdomain comparison: lowercase, without a leading "www."."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")

#: Longest a page waits for its own "load" event after navigation has returned at "domcontentloaded".
_LOAD_EVENT_WAIT_MS = 10_000


async def _wait_for_load_event(page: Page, label: str, timeout_ms: int = _LOAD_EVENT_WAIT_MS) -> None:
    """_navigate_with_retry returns at "domcontentloaded" (a page that never reaches "load" must not read as
    unreachable). Detection must still see what the page does AFTER that: the settle window above was sized as
    "a short window past `load`", and a consent tool's loader, its banner and most tag managers run after
    domcontentloaded. Measured after that navigation change, on the same sites: lemonde.fr lost its consent tool
    (so a high "no consent mechanism" finding), masterclass.gignaati.com lost Accept/Reject, and pre-consent
    tracker counts fell. Bounded and never fatal: a page that does not reach "load" is scanned as it stands."""
    try:
        await page.wait_for_load_state("load", timeout=timeout_ms)
    except PlaywrightError:
        logger.info("%s did not reach the load event within %dms; scanning it as it stands", label, timeout_ms)

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
#
# Retries are now bounded by TIME and by KIND of error, not by a fixed count of three for everything (scanner/failures.py explains the three kinds). A
# transient failure is retried with a doubling wait (1, 2, 4 ... 30 s, with a little jitter) until the retry window (Settings.scanner_nav_retry_seconds_*)
# runs out; a refusal (the connection reset or emptied) only within a short window; a permanent error never. _NAV_MAX_ATTEMPTS is a safety net against a
# loop that somehow does not advance in time, not a limit anything is expected to reach.
_NAV_MAX_ATTEMPTS = 10
_NAV_BACKOFF_BASE_S = 1.0
_NAV_BACKOFF_MAX_S = 30.0
_NAV_REFUSED_WINDOW_S = 10.0

# A page, a context or a browser that is closed after a navigation that broke can fail to answer (observed: page.close() never returned after
# net::ERR_HTTP2_PROTOCOL_ERROR on zomato.com, so the scan sat until the whole-scan ceiling killed it). Closing is bounded; what does not close in time is left
# to the browser's own shutdown.
_CLOSE_TIMEOUT_S = 10.0

# Page attempts that did not become a scanned page (a navigation that failed, a redirect to a page already scanned or to another site) used to spend the page
# budget as if they had: a scan with a limit of 5 could end having scanned 1. The budget now counts SCANNED pages; attempts have their own ceiling, so a site
# whose pages mostly fail cannot keep the crawl going for ever.
_EXTRA_ATTEMPTS_MIN = 5

# A page that answers HTTP 404 or 410 is a definitive, cheap answer: the address is dead (a stale sitemap entry), not a site that is slow or refusing. Such attempts do not use up the attempt
# ceiling above, up to this many per scan (beyond it they count like any other, so a sitemap of thousands of dead addresses still stops). Seen live on masterclass.gignaati.com: nine dead sitemap
# pages used the whole ceiling and the scan covered 1 of its 5 pages.
_DEAD_PAGE_ALLOWANCE = 30
_DEAD_PAGE_STATUSES = frozenset({404, 410})

_nav_slots: dict[int, asyncio.Semaphore] = {}


def _max_concurrent_navigations() -> int:
    configured = get_settings().scanner_max_concurrent_navigations
    return configured if configured > 0 else min(32, max(6, 2 * (os.cpu_count() or 4)))


def _nav_slot() -> asyncio.Semaphore:
    """One limit on simultaneous navigations for the whole process, whichever scans they belong to (see Settings.scanner_max_concurrent_navigations)."""
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _nav_slots:
        _nav_slots[loop_id] = asyncio.Semaphore(_max_concurrent_navigations())
    return _nav_slots[loop_id]


async def _close_quietly(target, label: str) -> None:
    """target.close(), bounded: never raises and never waits longer than _CLOSE_TIMEOUT_S."""
    try:
        await asyncio.wait_for(target.close(), timeout=_CLOSE_TIMEOUT_S)
    except TimeoutError:
        logger.warning("Closing %s did not finish within %.0fs; leaving it to the browser's own shutdown", label, _CLOSE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- closing is cleanup, never a reason to fail a scan
        logger.debug("Closing %s failed: %s", label, exc)

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
# The same shadow-DOM walk as _SHADOW_DOM_TEXT_JS, but collecting ONLY clickable
# controls and consent-dialog containers -- the live counterpart to
# page_parser.extract_control_text(), which can only see the serialized HTML.
# Both are needed: a real banner (klaviyo.com/Transcend) lives in an open shadow root
# that page.content() never serializes, so without this its Accept/Reject buttons are
# invisible to detection even though consent_interactor can click them.
_SHADOW_DOM_CONTROL_TEXT_JS = """() => {
    const SEL = 'button, a, input, summary, label, [role=button], [role=dialog],' +
                '[role=alertdialog], [aria-modal=true]';
    const HINT = /(cookie|consent|gdpr|ccpa|cmp|privacy)/i;
    const BARE_ACCEPT = new Set(['accept', 'accept all', 'accept & close', 'accept and close', 'i accept', 'agree', 'i agree', 'allow', 'allow all', 'ok', 'okay', 'got it', 'accept cookies', 'allow cookies']);
    const BARE_REJECT = new Set(['decline', 'decline all', 'reject', 'reject all', 'deny', 'refuse', 'no thanks', 'i decline', 'decline cookies', 'reject cookies']);
    // Only what a visitor can see. A consent tool renders its second layer (the preference centre, with "Reject All Cookies") into the page hidden, so reading hidden controls
    // made every such banner look as if it offered Reject on its first layer (konicaminolta.com: Accept All + Cookie Settings visible, Reject All only inside the closed panel).
    function shown(el) {
        if (!el.getClientRects || !el.getClientRects().length) return false;           // display:none on the element or an ancestor
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function grab(root, out) {
        for (const el of root.querySelectorAll(SEL)) {
            if (!shown(el)) continue;
            out.push(el.innerText || el.textContent || '');
            for (const a of ['value', 'aria-label', 'title']) {
                const v = el.getAttribute && el.getAttribute(a);
                if (v) out.push(v);
            }
        }
        for (const el of root.querySelectorAll('[class], [id]')) {
            const key = (el.className || '') + ' ' + (el.id || '');
            if (HINT.test(key) && shown(el)) out.push(el.innerText || el.textContent || '');
        }
        // A cookie notice whose buttons say only "Accept" / "Decline" (greenply.com) is recognised by structure: a short, visible container named as cookie or consent UI, that speaks of
        // cookies, holding a button whose whole label is a bare accept (or decline) word. The same rule as page_parser.cookie_dialog_controls.
        let dialogAccept = false, dialogReject = false;
        for (const box of root.querySelectorAll('[class], [id]')) {
            const key = (box.className || '') + ' ' + (box.id || '');
            if (!/(cookie|consent|gdpr|ccpa)/i.test(key) || !shown(box)) continue;
            const text = (box.innerText || '').trim().toLowerCase();
            if (text.length < 15 || text.length > 700 || !text.includes('cookie')) continue;
            for (const c of box.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) {
                if (!shown(c)) continue;
                const label = ((c.innerText || c.value || c.getAttribute('aria-label') || '') + '').trim().toLowerCase().replace(/^[.! ]+|[.! ]+$/g, '');
                if (BARE_ACCEPT.has(label)) dialogAccept = true;
                if (BARE_REJECT.has(label)) dialogReject = true;
            }
        }
        if (dialogAccept) out.push('cookie-dialog-accept');
        if (dialogReject) out.push('cookie-dialog-reject');
    }
    function walk(root, depth, out) {
        if (depth > 6) return;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { grab(el.shadowRoot, out); walk(el.shadowRoot, depth + 1, out); }
        }
    }
    const out = [];
    grab(document, out);
    walk(document, 0, out);
    return out.join(' ');
}"""

# A shadow root's textContent includes its <style> and <script> text. A consent widget that keeps its stylesheet in its shadow root put about 50,000 characters of CSS into a
# policy page's text on the runs where the widget had loaded by then (digitalanumati.com: 7,808 characters on most runs, 60,000 and cut on one), and the policy text was cut
# before its sharing and retention sections. Only the text a reader sees is collected, as on the plain-HTTP path (policy.analysis._html_to_text).
_SHADOW_DOM_TEXT_JS = """() => {
    const SKIP = new Set(['STYLE', 'SCRIPT', 'NOSCRIPT', 'TEMPLATE']);
    function visibleText(root) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: (n) => {
                for (let p = n.parentNode; p && p !== root; p = p.parentNode) { if (p.nodeType === 1 && SKIP.has(p.tagName)) return NodeFilter.FILTER_REJECT; }
                return NodeFilter.FILTER_ACCEPT;
            },
        });
        const parts = [];
        while (walker.nextNode()) parts.push(walker.currentNode.nodeValue);
        return parts.join(' ');
    }
    function collect(root, depth, out) {
        if (depth > 6) return;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) {
                out.push(visibleText(el.shadowRoot));
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


_ROBOTS_MAX_REDIRECTS = 5


async def _robots_get(context: BrowserContext, url: str):
    """One robots.txt request, never following a redirect itself (see _fetch_robots), with the configured timeout and one more try after a timeout: a slow answer is the usual form
    of a site's bot protection, and the file is small."""
    timeout_ms = get_settings().scanner_robots_timeout_seconds * 1000
    try:
        return await context.request.get(url, timeout=timeout_ms, max_redirects=0)
    except Exception as exc:
        if "timeout" not in str(exc).lower():
            raise
        logger.info("robots.txt request to %s timed out after %d ms; trying once more", url, timeout_ms)
        return await context.request.get(url, timeout=timeout_ms, max_redirects=0)


async def _fetch_robots(context: BrowserContext, root_url: str, user_agent: str = "") -> tuple[RobotsRules, list[str]]:
    """Best-effort robots.txt fetch: (the rules that apply to the scanner, Sitemap: URLs), read as RFC 9309 says (scanner/robots.py: Allow, wildcards, the group written for this
    scanner by name, longest match wins). `Sitemap:` lines are not group-scoped, so they are collected wherever they appear; sitemap.py re-checks each one's scope.
    What each answer means (RFC 9309): 2xx is parsed (lines that make no sense are skipped); 4xx (404, 403, ...) is "unavailable", the site publishes no rules, so everything is allowed; more than
    five redirects is also "unavailable". Anything that leaves the permission unknown -- a network error or timeout, HTTP 5xx or 429, a redirect to another site or to an unsafe address -- is
    "unreadable", and settings.scanner_robots_unreadable_policy decides: "disallow" (default) raises RobotsUnreadable, "allow" proceeds with a WARNING."""
    parsed = urlparse(root_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    site = _registered_domain(parsed.netloc)
    rules = RobotsRules()
    unreadable = ""
    try:
        # `context.request` is a standalone API client, not the page-level network
        # stack -- it is NOT intercepted by `_guard_navigation` above. A hostile site's
        # own robots.txt could 30x this backend-issued request to an internal/metadata
        # address (SSRF via redirect), so the client never follows redirects
        # (max_redirects=0). RFC 9309 asks for up to five hops, and http->https or
        # bare->www is how many sites serve robots.txt, so they are followed here one
        # at a time, each hop checked as a navigation is (assert_safe_url) and kept on
        # the scanned site. Ignoring the redirect used to read as "no robots.txt".
        response = await _robots_get(context, robots_url)
        for _hop in range(_ROBOTS_MAX_REDIRECTS):
            location = (response.headers or {}).get("location") if 300 <= response.status < 400 else None
            if not location:
                break
            robots_url = urljoin(robots_url, location)
            if not _is_same_site(robots_url, site):
                unreadable = f"it redirects to another site, {urlparse(robots_url).netloc}"
                break
            await assert_safe_url(robots_url)
            response = await _robots_get(context, robots_url)
        if response.ok:
            rules = parse_robots(await response.text(), user_agent or get_settings().scanner_user_agent)
        elif response.status >= 500 or response.status == 429:
            unreadable = f"it answered HTTP {response.status}"
        elif 300 <= response.status < 400 and not unreadable:
            logger.info("robots.txt of %s redirected more than %d times or without a location: treated as unavailable, no restrictions", root_url, _ROBOTS_MAX_REDIRECTS)
    except Exception as exc:
        if is_certificate_error(exc):
            # Permanent: not "unreadable, retry later". The certificate the site presents is read (public fields only, scanner/tls_diagnostics.py) so the report can say which one failed and why.
            host = urlparse(root_url).hostname
            raise certificate_failure(root_url, first_line(exc), tls_sentence(host or "", await describe_certificate(host))) from exc
        unreadable = first_line(exc) or type(exc).__name__
    if unreadable:
        if get_settings().scanner_robots_unreadable_policy == "disallow":
            raise RobotsUnreadable(parsed.netloc, unreadable)
        logger.warning("Could not read robots.txt of %s (%s): the site's restrictions are not applied to this scan (SCANNER_ROBOTS_UNREADABLE_POLICY=allow)", root_url, unreadable)
    return rules, rules.sitemaps


def _is_same_site(url: str, site_registered: str) -> bool:
    """The crawl boundary, for followed links and sitemap entries alike: http(s) on the
    seed's registered domain. A sitemap is the scanned site's own (untrusted) input,
    so it gets no wider scope than a link on one of its pages would."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and _registered_domain(parsed.netloc) == site_registered


def _is_robots_disallowed(url: str, disallowed_prefixes: RobotsRules | set[str]) -> bool:
    """True when robots.txt does not let the scanner visit `url`. Takes the parsed rules, or (as before) a plain set of Disallow prefixes."""
    if isinstance(disallowed_prefixes, RobotsRules):
        return not disallowed_prefixes.allows(url)
    path = urlparse(url).path or "/"
    return any(path.startswith(prefix) for prefix in disallowed_prefixes)


def _no_pages_scanned(root_url: str, diagnostics: dict) -> ScanNotPossible:
    """Why a crawl that ended normally scanned no page, in plain words."""
    if diagnostics.get("pages_blocked_robots") and not diagnostics.get("pages_attempted"):
        return ScanNotPossible(
            f"The robots.txt of {urlparse(root_url).netloc} does not allow the scanner to visit {root_url} (or any page found on it), and the scanner honours "
            "robots.txt, so no page was scanned and nothing was assessed. Allow the scanner in robots.txt, or scan a page the site permits.", "robots_disallowed")
    return ScanNotPossible(
        f"No page of {root_url} could be scanned ({diagnostics.get('pages_failed', 0)} failed, {diagnostics.get('pages_timeout', 0)} timed out, "
        f"{diagnostics.get('pages_blocked_robots', 0)} blocked by robots.txt), so nothing was assessed.", "no_pages")


class _SeedUnreachable(Exception):
    """Internal signal that the seed/root URL itself failed to load -- distinct from a
    secondary page failure, which is fine to skip. Raised from inside a concurrent
    gather() batch and re-raised (as the original RuntimeError) by the caller once
    outside the batch, so a sibling task's own exception can't shadow it."""

    def __init__(self, url: str, cause: Exception | None, attempts: int = 1):
        self.url = url
        self.cause = cause
        self.attempts = attempts


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Upper bound on waiting for Set-Cookie header reads before a page closes. Reading a
# response's full headers is a separate round trip to the browser; a read that has not
# come back by then is dropped, which only costs that cookie its header attribution
# (it is still found by the jar read and attributed from there).
_SET_COOKIE_DRAIN_SECONDS = 5


def _watch_set_cookies(browser_page: Page, page_url: str) -> tuple[list[dict], list[asyncio.Task]]:
    """Records every cookie a Set-Cookie response header sets while `browser_page` is
    open, as {"name", "domain", "path", "page_url", "request_url", "observed_at"} --
    evidence of which page set it, for cookie_detector.attribute_new_cookies.
    Observation only: nothing about how the page loads changes. Await the returned
    tasks (via _drain_set_cookie_reads) before closing the page."""
    events: list[dict] = []
    pending: list[asyncio.Task] = []

    async def _read(response) -> None:
        observed_at = _now_iso()
        try:
            values = await response.header_values("set-cookie")
        except Exception:  # noqa: BLE001 -- page or response gone; attribution falls back to the jar read
            return
        for name, domain, path in parse_set_cookie_headers(values, response.url):
            events.append({
                "name": name, "domain": domain, "path": path, "page_url": page_url,
                "request_url": response.url, "observed_at": observed_at,
            })

    browser_page.on("response", lambda response: pending.append(asyncio.ensure_future(_read(response))))
    return events, pending


async def _drain_set_cookie_reads(pending: list[asyncio.Task]) -> None:
    if not pending:
        return
    _done, not_done = await asyncio.wait(pending, timeout=_SET_COOKIE_DRAIN_SECONDS)
    for task in not_done:
        task.cancel()


async def _navigate_with_retry(
    browser_page: Page, url: str, timeout_ms: int, window_s: float | None = None,
) -> tuple[object | None, str, Exception | None, int]:
    """goto() with retries that fit the failure (scanner/failures.py): a transient one (a timeout, a resolver blip, the site answering HTTP 408/425/429/500/502/
    503/504) is retried with a doubling wait until `window_s` runs out; a refusal (the connection reset or emptied) only within a short window; a permanent one
    (a bad certificate, an invalid address, any other HTTP error status) not at all. Never retries without end, and never hides the original error: it is
    returned so the caller can decide what a permanent failure means (fail the scan for the seed address, record and skip any other page) and persist the
    real cause.

    An HTTP error status is a failed navigation, not a page: before this, a 403 "Access Denied" or a 503 page was scanned as if it were the site and produced a
    score and findings about the error page.

    wait_until="domcontentloaded", not "load" -- measured live against a real site (honda2wheelersindia.com) whose "load" event hangs on slow third-party
    stragglers unrelated to the page itself (a live-chat widget, CDN-hosted banner media, ad beacons): "load" timed out on every page twice and on one of four
    the third time, while a direct domcontentloaded probe returned the complete, genuine page in 1.56s every time. domcontentloaded does not lose evidence
    quality here: the fixed _TRACKER_SETTLE_MS wait and the adaptive _progressive_scroll pass both run AFTER whichever milestone goto() resolves on.

    Returns (response, status, exception, attempts_used). status is "success", "failed" (any non-timeout failure, including an HTTP error status) or "timeout"
    (Playwright's own TimeoutError) -- distinguished so callers can record which."""
    window = float(window_s) if window_s is not None else float(get_settings().scanner_nav_retry_seconds_page)
    started = time.monotonic()
    last_exc: Exception | None = None
    status = "failed"
    attempt = 0
    while attempt < _NAV_MAX_ATTEMPTS:
        attempt += 1
        retry_after: float | None = None
        try:
            async with _nav_slot():   # waits its turn when the process is already loading as many pages as it should; the timeout below starts after the wait
                response = await browser_page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            code = getattr(response, "status", None)
            if isinstance(code, int) and code >= 400:
                headers = getattr(response, "headers", None)
                retry_after = parse_retry_after(headers.get("retry-after") if isinstance(headers, dict) else None)
                raise NavigationHttpError(code, url, retry_after)
            if await _page_is_a_block_page(browser_page):
                # HTTP 200, but the page is the site's bot-protection message: treated exactly like a 403 (no score and findings about somebody's block screen).
                raise NavigationHttpError(403, url, None)
            return response, "success", None, attempt
        except NavigationHttpError as exc:
            last_exc, status, retry_after = exc, "failed", exc.retry_after
        except PlaywrightTimeoutError as exc:
            last_exc, status = exc, "timeout"
        except Exception as exc:  # noqa: BLE001 -- any other navigation failure is classified below
            last_exc, status = exc, "failed"

        kind = classify_navigation_failure(last_exc)
        if kind == PERMANENT:
            return None, status, last_exc, attempt          # retrying cannot change the answer
        limit = min(window, _NAV_REFUSED_WINDOW_S) if kind in (REFUSED, DNS) else window
        backoff = retry_after if retry_after else min(_NAV_BACKOFF_MAX_S, _NAV_BACKOFF_BASE_S * 2 ** (attempt - 1)) * (1 + random.uniform(0, 0.25))
        if attempt >= _NAV_MAX_ATTEMPTS or time.monotonic() - started + backoff >= limit:
            return None, status, last_exc, attempt
        logger.warning(
            "Navigation attempt %d failed for %s (%s, %s): %s -- retrying in %.1fs",
            attempt, url, kind, status, first_line(last_exc), backoff,
        )
        await asyncio.sleep(backoff)
    return None, status, last_exc, attempt


# --- the homepage preview ---------------------------------------------------------------------------------------------------------
# One picture per scan: the first screen of the homepage, taken in the load that already runs for the pre-consent crawl (after the page has
# loaded and settled, before any scrolling), so it shows the site as a first-time visitor sees it, banner included. Taking it only READS the
# page: no click, no scroll, no cookie or storage change, no extra navigation, so the consent tests that follow are unaffected. It is bounded
# and fail-soft: whatever goes wrong becomes a status on the record, never an exception.
_SHOT_QUALITY = 70
_SHOT_RETRY_QUALITY = 45
_SHOT_MAX_BYTES = 450_000
_SHOT_MIN_BYTES = 2_500       # a picture this small is a blank page, not a website
_SHOT_TIMEOUT_MS = 8_000
# A page can answer, fire its load event and still be showing nothing: a single-page site draws itself afterwards. A picture taken then is a white
# rectangle that is large enough in bytes to pass the size test above, and it would be kept as the client's homepage (scan ef0cda3d stored one: a
# 1280x720 picture of nothing). So the capture first waits, bounded, until the page has something painted, and a picture whose pixels are all one
# shade is retaken and, if it stays that way, recorded as blank rather than as a preview.
_SHOT_PAINT_WAIT_MS = 6_000
_SHOT_PAINT_STEP_MS = 400
_SHOT_BLANK_RETRIES = 2
_SHOT_BLANK_RETRY_WAIT_MS = 1_500
# Reads the page and changes nothing: true when it shows some text or a picture, a video, a canvas or a graphic of real size.
_PAINTED_JS = """() => {
  const text = document.body ? (document.body.innerText || '').trim().length : 0;
  const media = [...document.querySelectorAll('img,video,canvas,svg')].filter((e) => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 40; }).length;
  return text >= 20 || media > 0;
}"""
# Runs on a throwaway blank page of the same browser (never on the scanned page): true when the picture's brightest and darkest points differ by under 6 of 255.
_UNIFORM_JS = """async (src) => {
  const img = new Image(); img.src = src; await img.decode();
  const c = document.createElement('canvas'); c.width = 64; c.height = 36;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0, 64, 36);
  const d = g.getImageData(0, 0, 64, 36).data; let lo = 255, hi = 0;
  for (let i = 0; i < d.length; i += 4) { const l = (d[i] + d[i + 1] + d[i + 2]) / 3; if (l < lo) lo = l; if (l > hi) hi = l; }
  return hi - lo < 6;
}"""
# A site that answers the scanner with a block, challenge or error page is not showing its homepage. That picture would put somebody
# else's error screen in the client's report, so it is not kept.
_BLOCK_TITLE = re.compile(
    r"access denied|^\s*(?:forbidden\s*$|request blocked|just a moment|attention required|security check|unusual traffic|verify you are human|"
    r"are you a (?:robot|human)|please complete the captcha|40[0-9]\b|50[0-9]\b)",
    re.IGNORECASE,
)


# A block page that answers 200 and has an ordinary title is told apart by what it says. Only a SHORT page is tested (a real page has far more text), so a
# privacy notice that mentions "access denied" in passing is never taken for a block.
_BLOCK_BODY = re.compile(
    r"automated (?:access|traffic|requests?) (?:is|are) not (?:permitted|allowed)|reference:\s*webdriver|(?:your )?access (?:to this (?:page|site|website) )?(?:has been|is) (?:denied|blocked)|"
    r"you (?:have been|are) blocked|unusual traffic from your|verify you are (?:a )?human|checking your browser before accessing|(?:this|the) page can only be used from a regular browser",
    re.IGNORECASE,
)
_BLOCK_BODY_MAX_CHARS = 1200


async def _page_is_a_block_page(page: Page) -> bool:
    """True when what the page shows is a bot-protection or access-denied message rather than the site. Never raises: a page that cannot be asked is not called blocked."""
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:  # noqa: BLE001 -- a failed read is "not known", never "blocked"
        return False
    text = text if isinstance(text, str) else ""
    return 0 < len(text.strip()) <= _BLOCK_BODY_MAX_CHARS and bool(_BLOCK_BODY.search(text))


async def _page_says_it_was_not_found(page: Page) -> bool:
    """True when the page is a "not found" page served with a success status (see scanner/soft_404.py). Never raises: a page that cannot be asked is not called missing."""
    try:
        info = await page.evaluate("() => ({title: document.title || '', text: (document.body && document.body.innerText) || ''})")
    except Exception:  # noqa: BLE001 -- a failed read is "not known", never "missing"
        return False
    if not isinstance(info, dict):
        return False
    return looks_not_found(info.get("title"), info.get("text"))


async def _wait_until_painted(page: Page) -> bool:
    """Waits, bounded, until the page shows something. True when it does, or when that cannot be told (never an obstacle to the capture)."""
    waited = 0
    while waited < _SHOT_PAINT_WAIT_MS:
        try:
            if await page.evaluate(_PAINTED_JS):
                return True
            await page.wait_for_timeout(_SHOT_PAINT_STEP_MS)
        except Exception:  # noqa: BLE001 -- a page that cannot be asked is photographed as it stands
            return True
        waited += _SHOT_PAINT_STEP_MS
    return False


async def _is_uniform(page: Page, data: bytes) -> bool | None:
    """True when the picture is one flat shade (nothing was drawn), False when it has content, None when that could not be told."""
    try:
        probe = await page.context.new_page()
        try:
            return bool(await probe.evaluate(_UNIFORM_JS, "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")))
        finally:
            await probe.close()
    except Exception:  # noqa: BLE001 -- unknown is not blank: the picture is kept
        return None


async def _capture_homepage_screenshot(page: Page, *, http_status: int | None, final_url: str | None) -> dict:
    """The homepage preview as a plain dict (it becomes a ScreenshotRecord): status ok / blocked / timeout / failed, a reason code, and for
    "ok" the JPEG as base64 with its size. Never raises."""
    info: dict = {"page_url": final_url, "http_status": http_status}
    try:
        try:
            title = (await page.title()) or ""
        except PlaywrightError:
            title = ""
        info["page_title"] = title
        if (http_status is not None and http_status >= 400) or _BLOCK_TITLE.search(title) or await _page_is_a_block_page(page):
            return {**info, "status": "blocked", "reason": "blocked"}
        shot = {"type": "jpeg", "full_page": False, "timeout": _SHOT_TIMEOUT_MS, "animations": "disabled", "caret": "hide"}
        await _wait_until_painted(page)

        async def take() -> bytes:
            picture = await page.screenshot(quality=_SHOT_QUALITY, **shot)
            if len(picture) > _SHOT_MAX_BYTES:
                picture = await page.screenshot(quality=_SHOT_RETRY_QUALITY, **shot)
            return picture

        data = await take()
        for attempt in range(_SHOT_BLANK_RETRIES + 1):
            if len(data) >= _SHOT_MIN_BYTES and await _is_uniform(page, data) is not True:
                break
            if attempt == _SHOT_BLANK_RETRIES:
                return {**info, "status": "failed", "reason": "blank"}
            await asyncio.sleep(_SHOT_BLANK_RETRY_WAIT_MS / 1000)
            data = await take()
        size = page.viewport_size or {"width": 1280, "height": 720}
        return {**info, "status": "ok", "reason": None, "image_b64": base64.b64encode(data).decode("ascii"),
                "width": int(size["width"]), "height": int(size["height"])}
    except PlaywrightTimeoutError:
        return {**info, "status": "timeout", "reason": "capture_timeout"}
    except Exception as exc:  # noqa: BLE001 -- a preview that cannot be taken must never fail the scan
        logger.info("Could not capture the homepage preview of %s: %s", final_url, exc)
        return {**info, "status": "failed", "reason": "capture_failed"}


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


#: Seconds one iframe's HTML may take to read. Playwright's Frame.content() has no timeout of its
#: own, and a frame that is busy, detached or served by a stalled third party can leave it waiting
#: until something above cancels it -- which, on a real scan, was the whole-scan timeout. This is a
#: per-call safeguard, not a scan time limit: a slow frame is skipped, the page is not failed.
_IFRAME_READ_TIMEOUT_SECONDS = 15


async def _collect_iframe_signal_evidence(page: Page) -> tuple[list[ParsedScript], str, str]:
    """Extends detect_consent_signal's inputs to also see same-page iframe content --
    page.content() (the crawler's only HTML source otherwise) returns ONLY the main
    frame's DOM, so a CMP banner whose markup lives entirely inside an iframe
    previously contributed zero script/text signal to classification. Bounded to
    _MAX_IFRAMES_FOR_SIGNAL_DETECTION child frames; best-effort -- a frame that can't
    be read (detached mid-read, cross-origin content restrictions) is skipped, never
    fatal to the page fetch."""
    scripts: list[ParsedScript] = []
    text_parts: list[str] = []
    control_parts: list[str] = []
    child_frames = [f for f in page.frames if f != page.main_frame][:_MAX_IFRAMES_FOR_SIGNAL_DETECTION]
    for frame in child_frames:
        try:
            html = await asyncio.wait_for(frame.content(), timeout=_IFRAME_READ_TIMEOUT_SECONDS)
        except PlaywrightError as exc:
            logger.debug("Could not read iframe content at %r: %s", frame.url, exc)
            continue
        except TimeoutError:
            logger.warning(
                "Skipping iframe %r: its content did not load within %ss", frame.url, _IFRAME_READ_TIMEOUT_SECONDS
            )
            continue
        parsed = parse_page(html, frame.url or page.url)
        scripts.extend(parsed.scripts)
        text_parts.append(parsed.visible_text)
        # An iframe-hosted CMP is the common case for several real vendors, so its
        # buttons have to reach consent detection as controls, not just as prose.
        control_parts.append(parsed.control_text)
    return scripts, " ".join(text_parts), " ".join(control_parts)


#: The names of the keys in browser storage (never the values: they routinely hold personal data).
_STORAGE_KEYS_JS = """() => { try { return { local: Object.keys(localStorage).slice(0, 60), session: Object.keys(sessionStorage).slice(0, 60) }; } catch (e) { return null; } }"""

#: For each form, whether each shown control starts ticked, in the order page_parser lists them (hidden inputs and buttons left out): the live `checked` property for a checkbox or radio
#: button, null for anything else. A form built by a script has no `checked` attribute in the markup, so the property is the only way to see a pre-ticked consent box.
_FORM_CHECKED_JS = """() => Array.from(document.querySelectorAll('form')).map(f =>
  Array.from(f.querySelectorAll('input, textarea, select'))
    .filter(e => !['hidden', 'submit', 'button'].includes(e.getAttribute('type')))
    .map(e => (e.tagName === 'INPUT' && ['checkbox', 'radio'].includes(e.getAttribute('type'))) ? !!e.checked : null))"""


#: A policy page built by a script can still be adding text when the fixed settle window ends: swaransoft.com/privacy was read as 24,449 characters on one scan and 9,177 on another, and the shorter read
#: missed the sentence on withdrawing consent, which was then scored as a gap in the site's notice. The text of a policy page is read again until it stops growing: a few short rereads, bounded, never
#: "wait until the page is quiet" (Functional Scope PF-09), and only for pages named like a policy so no other page pays for it.
_TEXT_REREADS = 5
_TEXT_REREAD_WAIT_MS = 900
_TEXT_GROWTH = 1.02
_TEXT_STABLE_READS = 2


async def _settled_text(page: Page, url: str) -> str:
    """The page's rendered text (_SHADOW_DOM_TEXT_JS). For a policy page, the longest of a few readings, taken until the length has not grown for two readings in a row (at most five rereads)."""
    best = await page.evaluate(_SHADOW_DOM_TEXT_JS)
    if not is_policy_page(url):
        return best
    stable = 0
    for _ in range(_TEXT_REREADS):
        await page.wait_for_timeout(_TEXT_REREAD_WAIT_MS)
        again = await page.evaluate(_SHADOW_DOM_TEXT_JS)
        stable = 0 if len(again) > len(best) * _TEXT_GROWTH else stable + 1
        best = again if len(again) > len(best) else best
        if stable >= _TEXT_STABLE_READS:
            break
    return best


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
    if not is_seed and settings.scanner_skip_secondary_page_images:
        await browser_page.route("**/*", ImageSkipper(_registered_domain(urlparse(root_url).netloc)))
    set_cookie_events, set_cookie_reads = _watch_set_cookies(browser_page, url)
    cmp_script_failures: list[dict] = []

    def _note_cmp_failure(request_url: str, resource_type: str, error: str) -> None:
        # A consent tool whose script cannot be fetched never runs, so its banner never appears. Only scripts of a recognised consent tool are recorded.
        if resource_type == "script" and match_cmp(script_src=request_url) is not None and not any(f["url"] == request_url for f in cmp_script_failures):
            cmp_script_failures.append({"url": request_url[:300], "error": error[:200]})

    browser_page.on("requestfailed", lambda req: _note_cmp_failure(req.url, req.resource_type, str(req.failure or "the request failed")))
    browser_page.on("response", lambda resp: _note_cmp_failure(resp.url, resp.request.resource_type, f"HTTP {resp.status}") if resp.status >= 400 else None)

    try:
        response, status, exc, attempts = await _navigate_with_retry(
            browser_page, url, settings.scanner_timeout_seconds * 1000,
            window_s=settings.scanner_nav_retry_seconds_seed if is_seed else settings.scanner_nav_retry_seconds_page,
        )
        if status != "success":
            logger.warning("Failed to load %s after %d attempt(s) (%s): %s", url, attempts, status, first_line(exc))
            if is_seed:
                raise _SeedUnreachable(url, exc, attempts=attempts)
            return {"url": url, "status": status, "attempts": attempts, "error": first_line(exc, 300), "http_status": getattr(exc, "status", None)}

        final_url = getattr(browser_page, "url", None) or url
        # Bounded settle window (see _TRACKER_SETTLE_MS) replacing "networkidle" --
        # gives async-loading trackers/tag-managers a fair, predictable chance to fire.
        await _wait_for_load_event(browser_page, url)
        await browser_page.wait_for_timeout(_TRACKER_SETTLE_MS)
        # A bot-protection message drawn after the page loaded (HTTP 200) is the site turning the scanner away, not the site: handled like a 403.
        if await _page_is_a_block_page(browser_page):
            blocked = NavigationHttpError(403, url, None)
            logger.warning("The page at %s is a bot-protection message, not the site: treated as blocked", url)
            if is_seed:
                raise _SeedUnreachable(url, blocked, attempts=attempts)
            return {"url": url, "status": "failed", "attempts": attempts, "error": first_line(blocked, 300), "http_status": 403}
        # A "page not found" page that answers 200 is not a page of the site: a missing privacy or terms page must be reported as missing, not scored as a notice that was found.
        if await _page_says_it_was_not_found(browser_page):
            missing = NavigationHttpError(404, url, None)
            logger.warning("The page at %s says it was not found although it answered 200: treated as missing", url)
            if is_seed:
                raise _SeedUnreachable(url, missing, attempts=attempts)
            return {"url": url, "status": "failed", "attempts": attempts, "error": "The page answered 200 but says it was not found", "http_status": 404}
        # The homepage preview: the seed page only, at the top of the page, before the scroll pass below moves it.
        screenshot = (
            await _capture_homepage_screenshot(browser_page, http_status=response.status if response else None, final_url=final_url)
            if is_seed else None
        )
        scroll_steps = await _progressive_scroll(browser_page, request_count=lambda: len(request_records))
        http_status = response.status if response else None
        html = await browser_page.content()
        try:
            checked_states = await browser_page.evaluate(_FORM_CHECKED_JS)
        except PlaywrightError as exc:
            logger.debug("Could not read which form controls start ticked on %s: %s", url, exc)
            checked_states = None
        try:
            storage_keys = await browser_page.evaluate(_STORAGE_KEYS_JS)
        except PlaywrightError:
            storage_keys = None
        global_vars_present = await browser_page.evaluate(
            """(candidates) => candidates.filter(name => typeof window[name] !== 'undefined')""",
            _CMP_GLOBAL_VAR_CANDIDATES,
        )
        iframe_scripts, iframe_text, iframe_control_text = await _collect_iframe_signal_evidence(browser_page)
        try:
            rendered_text = await _settled_text(browser_page, url)
            rendered_control_text = await browser_page.evaluate(_SHADOW_DOM_CONTROL_TEXT_JS)
        except PlaywrightError as exc:
            logger.debug("Could not collect shadow-DOM text for %s: %s", url, exc)
            rendered_text = ""
            rendered_control_text = None        # not collected: the serialised HTML's controls are then the fallback (an EMPTY string means "no visible controls", and is kept)
    except _SeedUnreachable:
        raise
    except Exception as exc:  # page loaded (or retries exhausted) but post-load processing failed
        logger.warning("Failed processing %s: %s", url, exc)
        if is_seed:
            raise _SeedUnreachable(url, exc, attempts=1) from exc
        return {"url": url, "status": "failed", "attempts": 1, "error": str(exc)}
    finally:
        await _drain_set_cookie_reads(set_cookie_reads)
        if not browser_page.is_closed():
            await _close_quietly(browser_page, "a crawled page")

    parsed = parse_page(html, url, checked_states)
    # Read-only observations for the language and security sections of the report (scanner/page_signals.py); they change nothing about the scan.
    page_signals = {
        "indic": indic_script_counts(rendered_text or parsed.visible_text),
        "control_indic": indic_script_counts(rendered_control_text or parsed.control_text),
        "html_lang": parsed.html_lang, "hreflang": parsed.hreflang,
        "headers": response_headers(response) if response is not None and is_seed else None,
    }
    return {
        "page_signals": page_signals,
        # Names only, never values (QS-18, GR-16); and what loaded over plain http on a page served over https.
        "storage_keys": storage_keys if isinstance(storage_keys, dict) else None,
        "insecure_requests": [u for u, _t in request_records if u.lower().startswith("http://")][:10] if (final_url or url).lower().startswith("https://") else [],
        "url": url, "status": "success", "attempts": attempts, "http_status": http_status, "title": parsed.title,
        "request_records": request_records, "parsed": parsed, "global_vars": set(global_vars_present), "cmp_script_failures": cmp_script_failures,
        "scroll_steps": scroll_steps, "iframe_scripts": iframe_scripts, "iframe_text": iframe_text,
        "iframe_control_text": iframe_control_text,
        "rendered_text": rendered_text,
        "rendered_control_text": rendered_control_text,
        "set_cookie_events": set_cookie_events,
        "final_url": final_url,
        "content_type": (response_headers(response).get("content-type") or "")[:200].strip() or None if response is not None else None,
        "frame_sources": _frameset_sources(html, url),
        "screenshot": screenshot,
    }


async def _crawl_full_site(
    context: BrowserContext, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str],
    sitemap_urls: list[str] | None = None,
    *,
    profile: ScanProfile | None = None,
    deadline_monotonic: float | None = None,
    should_stop: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[list[PageRecord], list[FormRecord], list[CookieRecord], list[TrackerRecord],
           list[PolicyRecord], ConsentSignalRecord, dict]:
    """Pass 1 (pre_consent): the full multi-page crawl and main evidence-gathering pass.
    Fetches up to settings.scanner_max_concurrent_pages pages at once (bounded, not
    unbounded -- matches the queue's own natural batching), rather than one page at
    a time. Also returns a diagnostics dict for scan-metadata reporting -- never
    silently dropped, never fabricated.

    The page budget (settings.scanner_max_pages) is spent on NAVIGATIONS only:
      - a robots-disallowed URL is recorded at discovery and never queued, so a
        restrictive robots.txt no longer shrinks the real crawl;
      - URLs are keyed on url_normalize.normalize_url, so `/about`, `/about/`,
        `/about#team` and `/about?utm_source=x` are one entry, not four slots;
      - the queue is ordered by url_normalize.priority (seed first, then privacy/
        cookie/legal/form pages ahead of blog noise), FIFO within a priority.
    `sitemap_urls` (from sitemap.discover_sitemap_urls) are queued alongside the seed
    under the same same-site rule as a followed link, and recorded as "sitemap".

    The diagnostics keep discovered / attempted / scanned apart: pages_discovered is
    every distinct same-site URL seen, pages_attempted is what cost a navigation,
    pages_scanned is what loaded, and pages_not_attempted_budget_exhausted is what was
    still queued when the budget ran out.

    `profile` (scan_profiles.ScanProfile) replaces the deployment-wide page budget with the
    profile's own: a number for the web profile, or None for "no fixed page-count cap". It does
    NOT remove any other protection -- every page still passes the same-site test, the robots
    check, URL de-duplication, the SSRF guard on each navigation, the per-navigation timeout
    and the concurrency bound. `deadline_monotonic` (a time.monotonic() value) is what keeps a
    scan with no page cap finite: once it passes, no new batch is started and the scan
    completes with what it has, recording `stop_reason="deadline"`. With no profile the
    historical behaviour (settings.scanner_max_pages, no crawl deadline) is unchanged.

    `should_stop` is polled between batches (never mid-navigation): when it returns True the
    crawl starts no further pages, finishes the batch in flight, and returns what it collected
    with `stop_reason="cancelled"`. That is how a scan cancelled through the API stops, without
    killing a browser mid-page -- which is what leaves orphaned Chromium processes."""
    site_domain = urlparse(root_url).netloc

    pages: list[PageRecord] = []
    forms: list[FormRecord] = []
    scripts_by_page = {}
    network_requests_by_page = {}
    all_links = []
    crawled_page_text = {}
    all_visible_text_parts = []
    all_control_text_parts = []
    all_cmp_script_failures: list[dict] = []
    all_scripts_flat = []
    detected_global_vars: set[str] = set()
    budget = settings.scanner_max_pages if profile is None else profile.max_pages
    # The public scan's ceiling is enforced HERE, where pages are actually launched, not only in the settings that
    # feed it: whatever number arrived, a web scan navigates to at most MAX_WEB_PAGES distinct pages.
    if budget is not None and (profile is None or profile.name != APPLICATION):
        budget = min(budget, MAX_WEB_PAGES)
    # Crawl controls exist only for an application scan. A web scan has none: its page cap and behaviour are exactly what they were.
    controls = profile.crawl if profile is not None and profile.name == APPLICATION else None
    tracker = InventoryTracker(max_urls=controls.max_urls if controls is not None else UNCONTROLLED_CAP)
    limiter = DomainRateLimiter(controls.requests_per_second) if controls is not None else None
    guard = ResourceGuard(controls.memory_budget_mb) if controls is not None else None
    diagnostics = {
        "page_limit": budget,
        "page_limit_mode": "capped" if budget is not None else "uncapped",
        "scan_profile": profile.name if profile is not None else None,
        "stop_reason": None,
        "pages_not_attempted_deadline_exceeded": 0,
        "pages_discovered": 0,
        "pages_attempted": 0,
        "pages_scanned": 0,
        "pages_failed": 0,
        "pages_timeout": 0,
        "pages_blocked_robots": 0,
        "pages_skipped_duplicate": 0,
        "pages_from_sitemap": 0,
        "pages_not_attempted_budget_exhausted": 0,
        # Why a discovered page was not scanned, one entry per page (capped), and the full count per reason. These
        # are NOT page records: only a page the scanner actually navigated to is one.
        "skipped_pages": [],
        "skipped_by_reason": {},
        # Other sites that a frameset page displays in place of content of its own. See _frameset_sources.
        "framed_sites": [],
        # <script src> tags present in the DOM that the browser does not run (a CMP's type="text/plain"
        # block). Not counted as trackers; recorded so a reviewer can see the CMP did block something.
        "script_tags_not_executed": 0,
        "total_scroll_steps": 0,
        "pages_excluded_depth_limit": 0,
        "crawl_controls": controls.as_options() if controls is not None else None,
    }

    # (priority, insertion order, url as first spelled, discovered_via). The insertion
    # counter keeps it FIFO within a priority and keeps the heap from comparing urls.
    queue: list[tuple[float, tuple[int, int, str], str, str]] = []
    seen_keys: set[str] = set()
    seen_spellings: set[str] = set()

    main_host = _host_key(root_url)

    def _skip(url: str, reason: str) -> None:
        diagnostics["skipped_by_reason"][reason] = diagnostics["skipped_by_reason"].get(reason, 0) + 1
        if len(diagnostics["skipped_pages"]) < _MAX_SKIPPED_PAGES_LISTED:
            diagnostics["skipped_pages"].append({"url": url, "reason": reason})

    seed_section = section_of(root_url)

    def _discover(url: str, via: str, depth: int = 0) -> None:
        if via != "seed" and _host_key(url) != main_host:
            # The scan stays on the entered main domain. A link to a subdomain or to another website is neither followed
            # nor recorded: it is not a page of this site and not a reason to spend one of the pages.
            return
        key = normalize_url(url)
        if key in seen_keys:
            tracker.discover(url, via, depth)       # already known: the inventory just learns one more source for it
            if url not in seen_spellings:
                seen_spellings.add(url)
                diagnostics["pages_skipped_duplicate"] += 1
                _skip(url, "duplicate_url")
            return
        seen_keys.add(key)
        seen_spellings.add(url)
        if tracker.discover(url, via, depth) is None:
            # The inventory is full (max_urls): this URL is counted in tracker.over_limit and not recorded or queued.
            return
        diagnostics["pages_discovered"] += 1
        if controls is not None and controls.max_depth is not None and depth > controls.max_depth:
            diagnostics["pages_excluded_depth_limit"] += 1
            tracker.exclude(url, "depth_limit")
            _skip(url, "depth_limit")
            return
        if _is_robots_disallowed(url, disallowed_prefixes):
            logger.info("Skipping %s (robots.txt disallow)", url)
            tracker.exclude(url, "robots_disallowed")
            # Not a page record: a robots-disallowed link used to be appended to `pages`, one per link, with no limit --
            # which is how a scan capped at 5 reported 12 or more "pages". The count and the reason are kept.
            diagnostics["pages_blocked_robots"] += 1
            _skip(url, "robots_disallowed")
            return
        # Within one priority class, pages of the entered address's own section (/in-en/) come before the same kind of page in another section (/ae-en/, /ar-es/). Only a tie-break: a
        # higher-priority page (a privacy page) anywhere still comes first. Seen live: a scan of konicaminolta.com/in-en/ spent four of its five pages on /contact of other countries.
        out_of_section = 0.5 if seed_section and section_of(url) != seed_section else 0.0
        rank: float = -1 if via == "seed" else effective_rank(url) + out_of_section
        heapq.heappush(queue, (rank, tie_break_key(url), url, via))
        if via == "sitemap":
            diagnostics["pages_from_sitemap"] += 1

    _discover(root_url, "seed")
    for url in sitemap_urls or ():
        _discover(url, "sitemap", SITEMAP_DEPTH)

    max_concurrent = controls.effective_concurrency if controls is not None else settings.scanner_max_concurrent_pages
    # Where each cookie was first seen, filled after every completed group -- see
    # cookie_detector.attribute_new_cookies. Read-only bookkeeping: the cookie LIST
    # still comes from the single jar read after the crawl, exactly as before.
    cookie_observations: dict = {}

    stop_reason = "queue_exhausted"

    scanned_keys: set[str] = set()
    framed_sites: set[str] = set()

    consecutive_timeouts = 0
    seed_processed = False

    def _record_result(url: str, via: str, result: dict) -> None:
        nonlocal consecutive_timeouts, seed_processed
        if via == "seed":
            seed_processed = True
        page_local_id = f"page-{len(pages)}"
        if via != "seed":
            consecutive_timeouts = consecutive_timeouts + 1 if result["status"] == "timeout" else 0

        if result["status"] != "success":
            # Never silently skip: a secondary page that failed after bounded
            # retries still gets an explicit PageRecord, distinguishing "timeout"
            # from any other "failed" -- never left out of the pages list.
            pages.append(PageRecord(
                local_id=page_local_id, url=url, title=None, http_status=result.get("http_status"),
                discovered_via=result["status"],  # "failed" | "timeout"
            ))
            tracker.fail(url, result.get("error") or ("the page timed out" if result["status"] == "timeout" else "the page could not be loaded"), result.get("http_status"))
            diagnostics["pages_timeout" if result["status"] == "timeout" else "pages_failed"] += 1
            if via != "seed" and result["status"] == "failed" and result.get("http_status") in _DEAD_PAGE_STATUSES:
                diagnostics["pages_dead"] = diagnostics.get("pages_dead", 0) + 1
            return

        if via != "seed":
            # Where the navigation actually ended. A redirect can land on a page that was already scanned (counted
            # once, not twice) or on another host entirely (not a page of the main domain); neither brings in evidence.
            final_url = result.get("final_url") or url
            if _host_key(final_url) != main_host:
                # Another website is not a page of this site and is never listed: only the reason is kept, not where it went.
                tracker.exclude(url, "redirected_to_another_site")
                return
            if normalize_url(final_url) in scanned_keys:
                tracker.exclude(url, "redirected_to_already_scanned_page", final_url=final_url, http_status=result.get("http_status"))
                _skip(url, "redirected_to_already_scanned_page")
                return
        sig = result.get("page_signals") or {}
        merge_language(diagnostics.setdefault("language_signals", {}), indic=sig.get("indic") or {}, control_indic=sig.get("control_indic") or {},
                       html_lang=sig.get("html_lang"), hreflang=sig.get("hreflang") or [], is_seed=via == "seed")
        merge_security(diagnostics.setdefault("security_signals", {}), final_url=result.get("final_url") or url, headers=sig.get("headers"), is_seed=via == "seed")
        if via == "seed" and result.get("screenshot") is not None:
            # Carried out of the crawl beside the diagnostics, and removed from them before they are stored (run_scan): a picture is not a diagnostic.
            diagnostics["homepage_screenshot"] = result["screenshot"]
        for frame_src in result.get("frame_sources") or []:
            if _host_key(frame_src) != main_host:
                framed_sites.add(frame_src)
        scanned_keys.add(normalize_url(result.get("final_url") or url))
        scanned_keys.add(normalize_url(url))
        seen_keys.add(normalize_url(result.get("final_url") or url))
        diagnostics["pages_scanned"] += 1
        parsed = result["parsed"]
        detected_global_vars.update(result["global_vars"])
        pages.append(PageRecord(
            local_id=page_local_id, url=url, title=result["title"], http_status=result["http_status"],
            discovered_via=via, final_url=result.get("final_url"), content_type=result.get("content_type"),
        ))
        forms.extend(detect_forms(page_local_id, parsed.forms))
        # What the pages show about the notice, children and forms (read-only observations, scanner/page_signals.py): they feed Scanner Score A5, D1, D2 and G1.
        diagnostics.setdefault("banner_by_page", [])
        if len(diagnostics["banner_by_page"]) < 60:
            diagnostics["banner_by_page"].append({"url": url[:300], "banner": banner_on_page(
                scripts=[*parsed.scripts, *result["iframe_scripts"]], visible_text=result["rendered_text"] or parsed.visible_text, global_vars=sorted(result["global_vars"]),
                control_text=result.get("rendered_control_text") if result.get("rendered_control_text") is not None else parsed.control_text)})
        merge_storage_keys(diagnostics.setdefault("storage_signals", {}), result.get("storage_keys"))
        merge_mixed_content(diagnostics.setdefault("mixed_content", {}), page_url=url, has_form=bool(parsed.forms), insecure=result.get("insecure_requests") or [])
        merge_notice_links(diagnostics.setdefault("notice_link_signals", {}), page_url=url, has_link=links_to_privacy_notice(parsed.links))
        merge_child_surface(diagnostics.setdefault("child_surfaces", []), page_url=url, signal=child_directed_signal(url, result["title"], result["rendered_text"] or parsed.visible_text))
        for form_record, parsed_form in zip(forms[-len(parsed.forms):] if parsed.forms else [], parsed.forms):
            if len(diagnostics.setdefault("form_signals", [])) < 60:
                diagnostics["form_signals"].append({"page_url": url[:300], "selector": form_record.selector, "notice_link": parsed_form.notice_link})
        scripts_by_page[page_local_id] = parsed.scripts + result["iframe_scripts"]
        network_requests_by_page[page_local_id] = result["request_records"]
        all_links.extend(parsed.links)
        # The rendered text (the whole page, shadow roots included), not the 5,000-character excerpt: a policy found among the crawled
        # pages is kept in full for the content analysis. policy_detector is the only reader of this dictionary.
        crawled_page_text[url] = result["rendered_text"] or parsed.full_text or parsed.visible_text
        # rendered_text (document.body.innerText + every open shadow root's text,
        # see _SHADOW_DOM_TEXT_JS) is a strict superset of parsed.visible_text for
        # THIS purpose -- used only for consent-signal keyword detection, not for
        # crawled_page_text/policy_detector above, which is unaffected.
        all_visible_text_parts.append(result["rendered_text"] or parsed.visible_text)
        # Consent detection reads ONLY this -- see detect_consent_signal.
        all_cmp_script_failures.extend(f for f in result.get("cmp_script_failures", []) if f not in all_cmp_script_failures)
        rendered_controls = result.get("rendered_control_text")
        all_control_text_parts.append(rendered_controls if rendered_controls is not None else parsed.control_text)
        if result.get("iframe_control_text"):
            all_control_text_parts.append(result["iframe_control_text"])
        if result["iframe_text"]:
            all_visible_text_parts.append(result["iframe_text"])
        all_scripts_flat.extend(parsed.scripts)
        all_scripts_flat.extend(result["iframe_scripts"])
        diagnostics["script_tags_not_executed"] += sum(1 for sc in parsed.scripts if sc.src and not sc.executes)
        diagnostics["total_scroll_steps"] += result["scroll_steps"]

        # A page loaded with a good status is only COMPLETED when its content was extracted: a page with no text, or a file that is not HTML, is PARTIALLY_SCANNED.
        page_text = result["rendered_text"] or parsed.full_text or parsed.visible_text
        tracker.finish(
            url, final_url=result.get("final_url"), title=result["title"], http_status=result["http_status"], content_type=result.get("content_type"),
            incomplete_reason=incomplete_extraction(text_length=len((page_text or "").strip()), content_type=result.get("content_type")),
        )
        child_depth = (tracker.depth_of(url) or 0) + 1
        for link in parsed.links:
            _discover(link.href, "link", child_depth)

    # Rolling pool, not fixed lockstep batches: a page is launched the instant a
    # slot frees up, rather than waiting for every other page in its cohort to
    # finish first. Fixed batches made one slow/retried page (up to ~93s: all
    # _NAV_MAX_ATTEMPTS attempts timing out at scanner_timeout_seconds each, plus
    # backoff) stall the other (max_concurrent - 1) pages' slots even though they'd
    # long since finished -- measured live on a 25-page/concurrency-4 scan where a
    # single blocked page dominated its whole batch's wall time. A rolling pool
    # keeps exactly max_concurrent navigations in flight at all times (budget
    # permitting), so a slow page only ever blocks its own slot.
    in_flight: dict[asyncio.Task, tuple[str, str]] = {}

    # A page that failed, timed out or redirected away used to spend the budget as if it had been scanned, so a scan limited to 5 could end having scanned 1 (hdfcbank.com,
    # icicibank.com). The limit is on SCANNED pages; the attempts it may take to get them are limited separately.
    attempt_ceiling = None if budget is None else budget + max(_EXTRA_ATTEMPTS_MIN, budget)

    def _budget_left() -> bool:
        # `budget` is None for an uncapped (application-profile) scan: there is no page limit to run out of.
        if budget is None:
            return True
        dead_not_counted = min(diagnostics.get("pages_dead", 0), _DEAD_PAGE_ALLOWANCE)
        return (diagnostics["pages_scanned"] + len(in_flight) < budget
                and diagnostics["pages_attempted"] - dead_not_counted + len(in_flight) < attempt_ceiling)

    launched_paths: set[str] = set()
    # A public (capped) scan reads one page of each kind (Functional Scope QS-11): privacy, cookie policy, terms, contact, a page with a form, and the most prominent remaining page. A second page
    # of a kind already read or in flight waits; it is read only if the first of that kind failed to load, which frees the kind. An uncapped (application) scan reads every page and is unchanged.
    one_per_category = budget is not None
    covered_categories: set[str] = {"seed"}
    category_of_url: dict[str, str] = {}
    waiting: dict[str, list[tuple]] = {}

    async def _launch_page(url: str) -> dict:
        # An application scan spaces its navigation starts (requests per second against the domain); a web scan has no limiter and starts at once, as before.
        if limiter is not None:
            await limiter.acquire()
        return await _fetch_one_page(context, url, root_url, settings)

    def _fill_pool() -> None:
        # The entered address is loaded ALONE first. The pages are chosen by priority (privacy and cookie pages first), but the links that rank highest are only
        # found when the seed has loaded; with the pool already full of the sitemap's addresses, a slow seed let the fastest pages (often dead sitemap entries) use the
        # whole attempt budget before any priority link was known, so which pages were scanned depended on timing (heavy pages: 10 of 100 scans of the same kind of site
        # skipped the privacy page). Waiting for the seed costs one page load and makes the choice the same every time.
        while queue and len(in_flight) < max_concurrent and _budget_left() and (seed_processed or not in_flight):
            if guard is not None and guard.over_budget():
                break       # memory is over the scan's budget: launch nothing until a page in flight finishes and frees some (recorded in guard.events)
            _rank, _n, url, via = heapq.heappop(queue)
            if one_per_category and via != "seed":
                kind = category_of(url)
                # Only the five named kinds are limited to one page. "Remaining" pages are not a kind of evidence: they fill what is left, most prominent first, so a site with few policy pages still
                # uses its budget (a scan of hdfcbank.com once scanned one page of five).
                if kind == "remaining":
                    category_of_url[url] = kind
                elif kind in covered_categories:
                    waiting.setdefault(kind, []).append((_rank, _n, url, via))
                    continue
                else:
                    covered_categories.add(kind)
                    category_of_url[url] = kind
            variant_of = query_variant_of(url) if via != "seed" else None
            if variant_of is not None and variant_of in launched_paths:
                # /blog?page=2 after /blog: the same page with another query string. It would spend one of the pages on content already seen.
                tracker.exclude(url, "query_variant_of_known_page")
                _skip(url, "query_variant_of_known_page")
                continue
            launched_paths.add(path_without_query(url))
            tracker.start(url)
            in_flight[asyncio.create_task(_launch_page(url))] = (url, via)

    async def _check_stop() -> str | None:
        """Why no NEW page may start, or None. Checked between completions, never inside a navigation: a
        page already in flight finishes (each is bounded by its own navigation timeout) and none is launched."""
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return "deadline"
        limit = settings.scanner_stop_after_consecutive_timeouts
        if limit and consecutive_timeouts >= limit:
            return "slow_site"
        if should_stop is not None and await should_stop():
            return "cancelled"
        return None

    stop_launching = await _check_stop()
    if stop_launching is None:
        _fill_pool()
    while in_flight:
        done, _pending = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
        group = [(*in_flight.pop(task), task) for task in done]
        diagnostics["pages_attempted"] += len(group)

        results: list[tuple[str, str, dict]] = []
        for url, via, task in group:
            try:
                results.append((url, via, task.result()))
            except _SeedUnreachable as exc:
                # A secondary page's own failure is recorded as an explicit
                # failed/timeout PageRecord (never raised) rather than propagating
                # here, so this can only be the seed URL -- safe to fail the whole
                # scan. Cancel every other in-flight navigation first so none are
                # left orphaned (and their pages properly closed via _fetch_one_page's
                # own finally block running under cancellation).
                for other in in_flight:
                    other.cancel()
                if in_flight:
                    await asyncio.gather(*in_flight.keys(), return_exceptions=True)
                raise explain_seed_failure(exc.url, exc.cause, exc.attempts) from exc.cause

        try:
            jar = await context.cookies()
        except PlaywrightError as exc:
            logger.debug("Could not read cookies after a page group for %s: %s", root_url, exc)
        else:
            cookie_observations.update(attribute_new_cookies(
                jar, set(cookie_observations),
                batch_page_urls=[u for u, _v, r in results if r["status"] == "success"]
                or [u for u, _v, _r in results],
                header_events=[e for _u, _v, r in results for e in r.get("set_cookie_events", [])],
                observed_at=_now_iso(), consent_state="pre_consent",
            ))

        for url, via, result in results:
            _record_result(url, via, result)
            if one_per_category and result["status"] != "success" and url in category_of_url:
                # The page of this kind did not load: the kind is free again, and the next page of that kind (if one was waiting) may be read.
                freed = category_of_url.pop(url)
                covered_categories.discard(freed)
                for entry in waiting.pop(freed, []):
                    heapq.heappush(queue, entry)

        if stop_launching is None:
            stop_launching = await _check_stop()
        if stop_launching is None:
            _fill_pool()

    if stop_launching is not None:
        stop_reason = stop_launching
    elif queue and guard is not None and guard.is_throttled:
        stop_reason = "resource_limit"
    elif queue and not _budget_left():
        stop_reason = "page_limit" if budget is not None and diagnostics["pages_scanned"] >= budget else "attempt_limit"

    diagnostics["framed_sites"] = sorted(framed_sites)
    diagnostics["stop_reason"] = stop_reason
    _not_reached = {"page_limit": "page_limit", "deadline": "time_limit", "cancelled": "scan_cancelled", "slow_site": "slow_site", "resource_limit": "resource_limit"}.get(stop_reason, "not_reached")
    for kind, held in waiting.items():
        for _rank, _n, held_url, _via in held:
            # A page of a kind already read: not read, with that reason (the report shows it, never a bare "not reached").
            tracker.exclude(held_url, "category_already_covered")
            _skip(held_url, "category_already_covered")
    for _rank, _n, queued_url, _via in sorted(queue):
        _skip(queued_url, _not_reached)
    # Discovered is not scanned: what the crawl never started stays PENDING, with the reason it was not reached.
    tracker.leave_pending({u: _not_reached for u in tracker.pending_urls()})
    diagnostics["url_inventory_counts"] = tracker.counts()
    diagnostics["discovery_limit"] = {"max_urls": controls.max_urls if controls is not None else UNCONTROLLED_CAP, "urls_not_recorded": tracker.over_limit}
    diagnostics["resource_events"] = guard.events if guard is not None else []
    diagnostics["url_inventory_records"] = tracker.records()      # carried to the ScanResult and removed from the diagnostics before they are stored
    if stop_reason == "deadline":
        diagnostics["pages_not_attempted_deadline_exceeded"] = len(queue)
        diagnostics["pages_not_attempted_budget_exhausted"] = 0
    else:
        diagnostics["pages_not_attempted_budget_exhausted"] = len(queue)

    # A scan that scanned nothing is not a scan. It used to be reported as completed, with no pages and no reason (npci.org.in: its robots.txt disallows every
    # crawler but named search engines, which the scanner honours, so every address was skipped and the scan "finished" in two seconds). The reason is stated.
    if diagnostics["pages_scanned"] == 0 and stop_reason in ("queue_exhausted", "page_limit", "attempt_limit"):
        raise _no_pages_scanned(root_url, diagnostics)

    raw_cookies = await context.cookies()
    cookies = detect_cookies(raw_cookies, site_domain)
    trackers = detect_trackers(
        site_domain=site_domain, scripts_by_page=scripts_by_page,
        network_requests_by_page=network_requests_by_page,
    )
    policies = detect_policies(all_links, crawled_page_text, site_domain)
    # A privacy or terms link that leads to a missing page (HTTP 404, or a 200 "page not found") is not a policy that was found.
    missing_pages = {normalize_url(p.url) for p in pages if p.discovered_via == "failed" and p.http_status in (404, 410)}
    if missing_pages:
        policies = [p for p in policies if normalize_url(p.url) not in missing_pages]
    consent_signal = detect_consent_signal(
        scripts=all_scripts_flat, visible_text=" ".join(all_visible_text_parts),
        detected_global_vars=sorted(detected_global_vars),
        control_text=" ".join(all_control_text_parts),
    )
    if consent_signal.mechanism_type == "cmp" and all_cmp_script_failures:
        consent_signal.evidence = {**consent_signal.evidence, "cmp_script_failure": all_cmp_script_failures[0]}

    cookies = [
        c.model_copy(update={
            "consent_states": ["pre_consent"],
            "observations": _observation_for(c, cookie_observations),
        })
        for c in cookies
    ]
    trackers = [t.model_copy(update={"consent_states": ["pre_consent"]}) for t in trackers]

    return pages, forms, cookies, trackers, policies, consent_signal, diagnostics


def _observation_for(cookie: CookieRecord, observations: dict) -> list:
    found = observations.get(cookie_key(cookie.name, cookie.domain, cookie.path))
    return [found] if found else []


async def _crawl_single_page_with_interaction(
    context: BrowserContext,
    root_url: str,
    settings: Settings,
    disallowed_prefixes: RobotsRules | set[str],
    consent_state: str,
    click_fn: Callable[[Page], Awaitable[str]],
) -> tuple[list[CookieRecord], list[TrackerRecord], str, bool, int, dict | None]:
    """Pass 2/3 (post_accept / post_reject): homepage only, in a fresh context --
    establish the state (click), collect baseline evidence (settle), scroll (bounded),
    then capture evidence again so trackers that only fire post-scroll are attributed
    to this consent state too, not missed entirely.

    Returns (cookies, trackers, interaction_status, confirmed, scroll_steps, gate) -- `gate` is
    consent_interactor.pass_consent_gate's record, or None when the page showed no gate.
    `confirmed` is True only when the click was dispatched AND the banner's control is
    observably gone afterwards (consent_interactor.confirm_interaction_completed).
    interaction_status
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
        return [], [], "page_unreachable", False, 0, None

    site_domain = urlparse(root_url).netloc
    request_records: list[tuple[str, str]] = []
    browser_page = await context.new_page()
    browser_page.on(
        "request", lambda req, _r=request_records: _r.append((req.url, req.resource_type))
    )
    set_cookie_events, set_cookie_reads = _watch_set_cookies(browser_page, root_url)

    scripts: list = []
    interaction_status = "page_unreachable"
    confirmed = False
    scroll_steps = 0
    gate: dict | None = None
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
            # _navigate_with_retry now returns at "domcontentloaded". A consent banner is injected by a script that
            # runs after that (the Consiva SDK defers its init to DOMContentLoaded, then fetches its config; an age
            # gate is shown first), so clicking after only the settle window below found no banner at all:
            # masterclass.gignaati.com went from Accept and Reject both clicked to "cmp_not_automatable" for both.
            # The page's own "load" event is what the interaction used to wait for; bounded, and never fatal.
            await _wait_for_load_event(browser_page, root_url, _INTERACTION_LOAD_WAIT_MS)
            await browser_page.wait_for_timeout(_TRACKER_SETTLE_MS)  # see _TRACKER_SETTLE_MS docstring above
            gate = await pass_consent_gate(browser_page)
            interaction_status = await click_fn(browser_page)
            if interaction_status == "not_found":
                await browser_page.wait_for_timeout(_BANNER_RETRY_WAIT_MS)
                gate = await pass_consent_gate(browser_page) or gate
                interaction_status = await click_fn(browser_page)
            if interaction_status == "clicked":
                await browser_page.wait_for_timeout(2000)  # let post-click network activity settle -- separate from
                # _TRACKER_SETTLE_MS above: this one is conditional on an actual consent decision having just
                # fired, giving *that* specific action's downstream tag-manager effects time to propagate
                # "clicked" only means a click was dispatched without raising. Whether it
                # took effect is a separate observation (consent_interactor's docstring).
                confirmed = await confirm_interaction_completed(
                    browser_page, accept=consent_state == "post_accept"
                )
            scroll_steps = await _progressive_scroll(browser_page, request_count=lambda: len(request_records))
            html = await browser_page.content()
            scripts = parse_page(html, root_url).scripts
    except Exception as exc:  # noqa: BLE001 — this pass is best-effort; failure is recorded, not fatal
        logger.warning("Failed %s interaction pass for %s: %s", consent_state, root_url, exc)
    finally:
        await _drain_set_cookie_reads(set_cookie_reads)
        if not browser_page.is_closed():
            await _close_quietly(browser_page, "a crawled page")

    raw_cookies = await context.cookies()
    cookies = detect_cookies(raw_cookies, site_domain)
    # A fresh context that loaded only the homepage, so every cookie here came from it.
    cookie_observations = attribute_new_cookies(
        raw_cookies, set(), batch_page_urls=[root_url], header_events=set_cookie_events,
        observed_at=_now_iso(), consent_state=consent_state,
    )
    cookies = [c.model_copy(update={"observations": _observation_for(c, cookie_observations)}) for c in cookies]
    trackers = detect_trackers(
        site_domain=site_domain,
        scripts_by_page={"page-0": scripts},
        network_requests_by_page={"page-0": request_records},
    )

    cookies = [c.model_copy(update={"consent_states": [consent_state]}) for c in cookies]
    trackers = [t.model_copy(update={"consent_states": [consent_state]}) for t in trackers]
    return cookies, trackers, interaction_status, confirmed, scroll_steps, gate


def _merge_cookie_pass(base: list[CookieRecord], new: list[CookieRecord]) -> list[CookieRecord]:
    # cookie_key(), not the raw fields -- name+domain alone can collide (the same name
    # and domain with two different paths are two distinct cookies in the browser).
    by_key = {cookie_key(c.name, c.domain, c.path): i for i, c in enumerate(base)}
    for cookie in new:
        key = cookie_key(cookie.name, cookie.domain, cookie.path)
        if key in by_key:
            idx = by_key[key]
            existing = base[idx]
            merged = existing.consent_states + [s for s in cookie.consent_states if s not in existing.consent_states]
            base[idx] = existing.model_copy(update={
                "consent_states": merged,
                "observations": existing.observations + cookie.observations,
            })
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


#: Longest an interaction pass waits for the page's load event before looking for the banner.
_INTERACTION_LOAD_WAIT_MS = 15_000

#: A banner that is not there on the first look gets one more look after this long. Consent tools fetch their
#: configuration over the network before drawing anything, so a slow response reads as "no banner": one of about
#: a dozen scans of gignaati.com found no Reject control on the first try and clicked it on every other. One retry,
#: never a loop -- a site with no banner pays this wait once.
_BANNER_RETRY_WAIT_MS = 2_500

#: A page that loaded a known consent tool but showed no banner is looked at once more, in a fresh browser context, as a fresh page view. Consent tools decide per page view
#: whether to draw the banner, and some decide "no" when their configuration request is slow: the Consiva SDK gives a first-time visitor 800 ms and then shows nothing for that
#: page view (swaransoft.com, 27 Oct 2026: its console says "Banner config request timed out after 800ms. No banner will be shown for this pageview." in every load that
#: drew no banner; 0 of 36 loads run one at a time, 4 of 126 six at a time and 11 of 48 twelve at a time). Waiting longer cannot help: the decision is already made. A second page
#: view usually is not slowed the same way. One retry only, so a site whose tool draws no banner for a visitor (a region rule) pays one more pass, not a loop.
_BANNER_PASS_RETRIES = 1


def _a_consent_tool_was_loaded(trackers: list[TrackerRecord]) -> bool:
    """True when one of the page's third-party requests is a consent tool in the catalogue: its banner was expected."""
    return any(match_cmp(script_src=t.script_src) is not None for t in trackers)


async def _run_interaction_pass(
    browser, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str],
    consent_state: str, click_fn: Callable[[Page], Awaitable[str]],
) -> tuple[list[CookieRecord], list[TrackerRecord], str, bool, int, dict | None]:
    """Owns one interaction pass's whole context lifecycle (create, route-guard,
    close) so two of these can run concurrently via asyncio.gather() in run_scan()
    without sharing any state -- each gets its own fresh browser context, same as
    the previous sequential version did. This isolation is deliberately preserved,
    not optimized away: the three consent states must never leak cookies/storage/
    consent choices into each other."""
    result = None
    for attempt in range(1 + _BANNER_PASS_RETRIES):
        context = await browser.new_context(**_new_context_kwargs(settings))
        await context.route("**/*", _guard_navigation)
        try:
            result = await _crawl_single_page_with_interaction(
                context, root_url, settings, disallowed_prefixes, consent_state, click_fn
            )
        finally:
            await _close_quietly(context, f"the {consent_state} context")
        # Only the last attempt's evidence is kept: a page view that drew no banner may have let tags through that the banner would have held back, and those must not be
        # read as what the site does after Accept or Reject.
        if result[2] != "not_found" or not _a_consent_tool_was_loaded(result[1]) or attempt == _BANNER_PASS_RETRIES:
            break
        logger.info("The %s pass of %s loaded a consent tool but drew no banner; looking again in a fresh browser context", consent_state, root_url)
    return result


#: Most pages looked at again when something fired before consent (the crawl scans at most MAX_WEB_PAGES, so this is every page of a web scan).
_RECHECK_MAX_PAGES = MAX_WEB_PAGES


def _non_essential_pre_consent_hits(cookies: list[CookieRecord], trackers: list[TrackerRecord]) -> int:
    """How many analytics or marketing items the crawl saw before consent, classified the way the report will classify them."""
    from app.rules.consent_rules import classify_cookie, classify_tracker

    tracking = ("analytics", "marketing")
    return (
        sum(1 for t in trackers if "pre_consent" in t.consent_states and classify_tracker(t).category in tracking)
        + sum(1 for c in cookies if "pre_consent" in c.consent_states and classify_cookie(c).category in tracking)
    )


async def _recheck_one_load(browser, url: str, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str]) -> dict:
    """One page, loaded alone in a fresh browser context as a first-time visitor, nothing else running: did analytics or marketing fire before any consent choice?

    Returns {"url", "loaded", "fired", "requests", "cookies"}; `loaded` False means the page did not load, so the answer is unknown, never "did not fire"."""
    from app.rules.consent_rules import classify_cookie, classify_tracker

    tracking = ("analytics", "marketing")
    out = {"url": url, "loaded": False, "fired": False, "requests": 0, "cookies": 0}
    if _is_robots_disallowed(url, disallowed_prefixes):
        return out
    site_domain = urlparse(root_url).netloc
    context = await browser.new_context(**_new_context_kwargs(settings))
    await context.route("**/*", _guard_navigation)
    request_records: list[tuple[str, str]] = []
    try:
        page = await context.new_page()
        page.on("request", lambda req, _r=request_records: _r.append((req.url, req.resource_type)))
        _, nav_status, exc, _attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000)
        if nav_status != "success":
            logger.info("The clean re-check could not load %s: %s", url, exc)
            return out
        await _wait_for_load_event(page, url, _INTERACTION_LOAD_WAIT_MS)
        await page.wait_for_timeout(_TRACKER_SETTLE_MS)
        await _progressive_scroll(page, request_count=lambda: len(request_records))
        trackers = [classify_tracker(t) for t in detect_trackers(site_domain=site_domain, scripts_by_page={}, network_requests_by_page={"page-0": request_records})]
        cookies = [classify_cookie(c) for c in detect_cookies(await context.cookies(), site_domain)]
        out.update(
            loaded=True,
            requests=sum(1 for t in trackers if t.category in tracking),
            cookies=sum(1 for c in cookies if c.category in tracking),
        )
        out["fired"] = bool(out["requests"] or out["cookies"])
        return out
    except Exception as exc:  # noqa: BLE001 -- best effort: an unreadable re-check leaves the original observation standing
        logger.info("The clean re-check of %s failed: %s", url, exc)
        return out
    finally:
        await _close_quietly(context, "a re-check context")


#: A page that fires in its first clean load is loaded this many more times, each in a fresh context. The re-check is itself a page view with the same race as the crawl (a first load
#: right after the browser started fired on two of three pages of swaransoft.com, and none of the next eight clean loads did), so one more firing is not yet "it repeats".
_RECHECK_CONFIRMATION_LOADS = 2


async def _recheck_one_page(browser, url: str, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str]) -> dict:
    """A page's answer: a clean load; and, only when that load fired, two more. It "fired" when most of its clean loads did, so a single blip is recorded as a blip.

    Returns {"url", "loaded", "fired", "loads", "fired_loads", "requests", "cookies"}; `requests` and `cookies` are the highest counts seen in a load."""
    first = await _recheck_one_load(browser, url, root_url, settings, disallowed_prefixes)
    loads = [first]
    if first["loaded"] and first["fired"]:
        for _ in range(_RECHECK_CONFIRMATION_LOADS):
            counted_so_far = [load for load in loads if load["loaded"]]
            # The answer is "fired in most loads". When two loads have been read and both fired, a third cannot change it (2 of 3 or 3 of 3 is still most), so it is not spent.
            if len(counted_so_far) >= 2 and all(load["fired"] for load in counted_so_far):
                break
            loads.append(await _recheck_one_load(browser, url, root_url, settings, disallowed_prefixes))
    counted = [load for load in loads if load["loaded"]]
    fired_loads = sum(1 for load in counted if load["fired"])
    return {
        "url": url, "loaded": first["loaded"], "loads": len(counted), "fired_loads": fired_loads,
        "fired": bool(counted) and fired_loads * 2 > len(counted),
        "requests": max((load["requests"] for load in counted), default=0), "cookies": max((load["cookies"] for load in counted), default=0),
    }


async def _recheck_pre_consent(
    browser, pages: list[PageRecord], cookies: list[CookieRecord], trackers: list[TrackerRecord], root_url: str, settings: Settings,
    disallowed_prefixes: RobotsRules | set[str],
) -> dict | None:
    """Looks again, one page at a time and alone, when the crawl saw analytics or marketing before consent. None when it saw none (nothing to confirm).

    The crawl reads several pages at once, and a consent tool that gives its banner a short time to load (the Consiva SDK: 800 ms) can miss that time under load: the banner is
    then not drawn for that page view and the tags run unguarded (FR-20261027-D200 against -1DB8, one website, two results). A page view that fired once and not again is
    different evidence from one that fires every time, so the question is asked again under the best conditions the scanner can give. Every observation of the crawl is kept;
    this adds the answer. Which page a shared tag URL is attributed to is only the first page that loaded it, so every scanned page is looked at, not only that one."""
    crawl_hits = _non_essential_pre_consent_hits(cookies, trackers)
    if not crawl_hits:
        return None
    urls = [p.url for p in pages if p.discovered_via in ("seed", "link", "sitemap")][:_RECHECK_MAX_PAGES]
    results = [await _recheck_one_page(browser, url, root_url, settings, disallowed_prefixes) for url in urls]
    return {"checked_at": _now_iso(), "crawl_hits": crawl_hits, "pages": results, "reproduced": any(r["fired"] for r in results), "complete": bool(results) and all(r["loaded"] for r in results)}


def _reconcile_banner_presence(signal: ConsentSignalRecord, accept_status: str, reject_status: str) -> None:
    """A consent tool's page text only counts as a fact about its banner when banner wording was seen (consent_signal_detector). If an Accept or Reject control was then
    CLICKED, the banner was demonstrably on screen, so what its text said (no reject-all, per-purpose choices) is restored. Without a click it stays unknown."""
    if signal.mechanism_type != "cmp" or "clicked" not in (accept_status, reject_status):
        return
    ev = signal.evidence or {}
    if signal.has_reject_all is None and ev.get("text_has_reject_all") is not None:
        signal.has_reject_all = ev["text_has_reject_all"]
    if signal.has_granular_choices is None and ev.get("text_has_granular_choices") is not None:
        signal.has_granular_choices = ev["text_has_granular_choices"]


def _reconcile_reject_presence(signal: ConsentSignalRecord, reject_status: str, reject_confirmed: bool) -> None:
    """The pre-consent crawl reads the banner's text as a first-time visitor sees it. When a gate comes
    first (an age gate), "Reject All" is not on screen yet, so has_reject_all was recorded False and rule
    R-005 reported "no reject-all option" -- while the post-reject pass went on to find that very control,
    click it, and watch the banner record the choice. A Reject control that was found AND operated (clicked,
    with the banner observably responding) is evidence one exists; it is not left contradicted. Nothing is
    changed unless both hold, and an unconfirmed click never overrides what the page text showed."""
    if signal.has_reject_all is False and reject_status == "clicked" and reject_confirmed:
        signal.has_reject_all = True
        signal.evidence = {**signal.evidence, "reject_control_seen_via": "interaction_pass"}


async def _run_withdrawal(browser, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str], pages: list[PageRecord]) -> dict:
    """The withdrawal test in its own fresh browser session, with the same address guard as every other pass. Never raises: a test that cannot be run is recorded as not tested."""
    root_key = normalize_url(root_url)
    further = [p.url for p in pages if p.discovered_via in ("seed", "link", "sitemap") and normalize_url(p.url) != root_key and not _is_robots_disallowed(p.url, disallowed_prefixes)]

    async def goto(page: Page, url: str) -> bool:
        _, status, _exc, _attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000)
        return status == "success"

    try:
        context = await browser.new_context(**_new_context_kwargs(settings))
        await context.route("**/*", _guard_navigation)
        try:
            return await run_withdrawal_test(context, root_url, further, goto=goto)
        finally:
            await _close_quietly(context, "the withdrawal context")
    except Exception as exc:  # noqa: BLE001
        logger.warning("The withdrawal test for %s could not be started: %s", root_url, exc)
        return {"tested": False, "reason": "error", "requests_only": True}


def _session_tools(browser, settings: Settings):
    """(make_context, goto) for a fresh guarded browser session, the same as every other pass uses."""

    async def make_context():
        context = await browser.new_context(**_new_context_kwargs(settings))
        await context.route("**/*", _guard_navigation)
        return context

    async def goto(page: Page, url: str) -> bool:
        _, status, _exc, _attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000)
        return status == "success"

    return make_context, goto


async def _run_button_comparison(browser, root_url: str, settings: Settings) -> dict | None:
    make_context, goto = _session_tools(browser, settings)
    try:
        return await run_button_comparison(make_context, root_url, goto=goto)
    except Exception as exc:  # noqa: BLE001
        logger.warning("The Accept and Reject comparison for %s could not be started: %s", root_url, exc)
        return None


async def _run_reject_persistence(browser, root_url: str, settings: Settings, disallowed_prefixes: RobotsRules | set[str], pages: list[PageRecord]) -> dict:
    make_context, goto = _session_tools(browser, settings)
    root_key = normalize_url(root_url)
    further = [p.url for p in pages if p.discovered_via in ("seed", "link", "sitemap") and normalize_url(p.url) != root_key and not _is_robots_disallowed(p.url, disallowed_prefixes)]
    try:
        return await run_reject_persistence(make_context, root_url, further, goto=goto)
    except Exception as exc:  # noqa: BLE001
        logger.warning("The test of Reject on further pages for %s could not be started: %s", root_url, exc)
        return {"tested": False, "reason": "error", "non_essential_after_reject": [], "further_pages": [], "requests_only": True}


async def _run_purposes(browser, root_url: str, settings: Settings) -> dict:
    """The test of choices by purpose, each purpose in its own fresh browser session with the same address guard as every other pass. Never raises: a test that cannot be run is recorded as not tested."""

    async def make_context():
        context = await browser.new_context(**_new_context_kwargs(settings))
        await context.route("**/*", _guard_navigation)
        return context

    async def goto(page: Page, url: str) -> bool:
        _, status, _exc, _attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000)
        return status == "success"

    try:
        return await run_purpose_test(make_context, root_url, goto=goto)
    except Exception as exc:  # noqa: BLE001
        logger.warning("The test of choices by purpose for %s could not be started: %s", root_url, exc)
        return {"tested": False, "reason": "error", "purposes": [], "requests_only": True}


async def run_scan(
    root_url: str, settings: Settings | None = None, profile: ScanProfile | None = None,
    should_stop: Callable[[], Awaitable[bool]] | None = None,
) -> ScanResult:
    settings = settings or get_settings()
    safe_ips = await assert_safe_url(root_url)  # fail fast; _guard_navigation re-checks every hop below
    launch_args = _host_resolver_pin_args(urlparse(root_url).hostname, safe_ips)
    phase_clock = [time.monotonic()]

    def mark(phase: str) -> None:
        """Logs how long the phase that just ended took, so a slow scan can be traced to the phase that made it slow."""
        now = time.monotonic()
        logger.info("Scan phase %s of %s took %.1fs", phase, root_url, now - phase_clock[0])
        phase_clock[0] = now

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.scanner_headless, args=launch_args)
        try:
            context = await browser.new_context(**_new_context_kwargs(settings))
            await context.route("**/*", _guard_navigation)
            disallowed_prefixes, robots_sitemaps = await _fetch_robots(context, root_url)
            site_registered = _registered_domain(urlparse(root_url).netloc)

            async def consent_passes():
                """post_accept and post_reject are fully independent -- separate contexts (deliberately fresh, so neither pass's consent state leaks into the
                other), no shared mutable state between them or with the crawl, which uses `context`. Running them concurrently is a pure wall-clock win with no
                correctness change: same navigations, same click attempts, same detection logic."""
                return await asyncio.gather(
                    _run_interaction_pass(browser, root_url, settings, disallowed_prefixes, "post_accept", click_accept),
                    _run_interaction_pass(browser, root_url, settings, disallowed_prefixes, "post_reject", click_reject),
                )

            # They also do not depend on the crawl's result (only on the address and robots.txt), so they can start now and run beside it.
            passes_task = asyncio.ensure_future(consent_passes()) if settings.scanner_overlap_consent_passes else None
            try:
                sitemap_urls = await discover_sitemap_urls(
                    context, root_url, robots_sitemaps,
                    same_site=lambda url: _is_same_site(url, site_registered),
                )
                # The crawl deadline starts here, when the crawl does -- browser start-up and
                # robots/sitemap discovery above are covered by the profile's separate
                # allowance inside its overall timeout, not by this number.
                crawl_deadline = (
                    time.monotonic() + profile.crawl_deadline_seconds
                    if profile is not None and profile.crawl_deadline_seconds is not None
                    else None
                )
                pages, forms, cookies, trackers, policies, consent_signal, pre_consent_diagnostics = await _crawl_full_site(
                    context, root_url, settings, disallowed_prefixes, sitemap_urls=sitemap_urls,
                    profile=profile, deadline_monotonic=crawl_deadline, should_stop=should_stop,
                )
            except BaseException:
                # The crawl failed or the scan was cancelled: the passes beside it are no longer wanted.
                if passes_task is not None:
                    passes_task.cancel()
                    await asyncio.gather(passes_task, return_exceptions=True)
                raise
            finally:
                await _close_quietly(context, "the crawl context")
            mark("crawl")
            homepage_screenshot = pre_consent_diagnostics.pop("homepage_screenshot", None)
            url_inventory = pre_consent_diagnostics.pop("url_inventory_records", [])

            (
                (accept_cookies, accept_trackers, accept_status, accept_confirmed, accept_scroll_steps, accept_gate),
                (reject_cookies, reject_trackers, reject_status, reject_confirmed, reject_scroll_steps, reject_gate),
            ) = await (passes_task if passes_task is not None else consent_passes())
            mark("wait for the Accept and Reject passes")
            # After everything else has finished, so the re-check really is one page view with nothing else running.
            recheck = await _recheck_pre_consent(browser, pages, cookies, trackers, root_url, settings, disallowed_prefixes)
            mark("pre-consent re-check")
            # The Deep Scan also tests taking consent back, and whether that holds on two further pages (Scanner Score B5, B6). A Quick Scan does not.
            # Taking consent back is tested on every scan, Quick or Deep (scorecard P5): one fresh session, then two further pages.
            withdrawal = await _run_withdrawal(browser, root_url, settings, disallowed_prefixes, pages)
            mark("withdrawal test")
            purposes = await _run_purposes(browser, root_url, settings) if profile is not None and profile.name == APPLICATION else None
            # Accept and Reject measured side by side on every scan; Reject on further pages on a Deep Scan (scanner/reject_and_compare.py).
            button_comparison = await _run_button_comparison(browser, root_url, settings)
            mark("button comparison")
            reject_persistence = await _run_reject_persistence(browser, root_url, settings, disallowed_prefixes, pages) if profile is not None and profile.name == APPLICATION else None
        finally:
            await _close_quietly(browser, "the browser")

    cookies = _merge_cookie_pass(cookies, accept_cookies)
    cookies = _merge_cookie_pass(cookies, reject_cookies)
    trackers = _merge_tracker_pass(trackers, accept_trackers)
    trackers = _merge_tracker_pass(trackers, reject_trackers)

    consent_signal.evidence = {
        **consent_signal.evidence,
        "accept_interaction": _final_interaction_status(accept_status, consent_signal.mechanism_type),
        "reject_interaction": _final_interaction_status(reject_status, consent_signal.mechanism_type),
        # Read by consent_rules (R-003/R-011) and the prompt: a dispatched click whose
        # effect was never observed does not establish the consent state.
        "accept_click_confirmed": accept_confirmed,
        "reject_click_confirmed": reject_confirmed,
        # The one page Accept and Reject were tried on. The mechanism is detected across every crawled page, but the
        # interaction passes only exercise this one -- so "not automatable" can mean "no banner on THIS page".
        "interaction_page_url": root_url,
        # Whether what fired before consent fired again when a page was loaded alone (absent when nothing fired): read by the score's first consent check.
        **({"pre_consent_recheck": recheck} if recheck else {}),
        # Set only when the scanned page is a frameset that shows another site: see rule R-012.
        **({"framed_sites": pre_consent_diagnostics["framed_sites"]} if pre_consent_diagnostics.get("framed_sites") else {}),
        # Whether consent can be taken back and whether that is honoured (Deep Scan only; scanner/withdrawal.py).
        **({"withdrawal": withdrawal} if withdrawal else {}),
        # Whether each choice by purpose does only what it says (Deep Scan only; scanner/purposes.py).
        **({"purposes": purposes} if purposes else {}),
        **({"button_comparison": button_comparison} if button_comparison else {}),
        **({"reject_persistence": reject_persistence} if reject_persistence else {}),
        # What the scanner answered before the consent choice could be shown (an age gate), per pass.
        # Absent when no gate was on the page.
        **({"consent_gate": {"post_accept": accept_gate, "post_reject": reject_gate}} if (accept_gate or reject_gate) else {}),
    }

    _reconcile_banner_presence(consent_signal, accept_status, reject_status)
    _reconcile_reject_presence(consent_signal, reject_status, reject_confirmed)

    scan_diagnostics = {
        **pre_consent_diagnostics,
        "accept_scroll_steps": accept_scroll_steps,
        "reject_scroll_steps": reject_scroll_steps,
    }
    if settings is None or getattr(settings, "scanner_tls_assessment", True):
        # The certificate and TLS version of the address the site was served from (scorecard P11, P12): one verified handshake, no request. https when the home page was served over https.
        served_https = (pre_consent_diagnostics.get("security_signals") or {}).get("homepage_https")
        host = urlparse(root_url).hostname or ""
        tls_url = root_url if served_https is None else (f"https://{host}" if served_https else f"http://{host}")
        scan_diagnostics["tls_signals"] = await assess_site_tls(tls_url)

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
        screenshot=ScreenshotRecord(**homepage_screenshot) if homepage_screenshot else None,
        url_inventory=url_inventory,
    )


def _scan_budget_seconds(settings: Settings | None) -> float:
    """Overall wall-clock ceiling for one scan, shared by BOTH platform paths.

    The formula lives on Settings.scan_budget_seconds, because jobs/queue.py needs the
    same number to keep its stale-job threshold above it -- see that property's
    docstring. It is still the per-page navigation timeout times the page cap, plus
    headroom for browser startup and the two consent-interaction passes; it previously
    lived inline in the Windows subprocess call only, which is exactly how the Linux
    path ended up with no ceiling at all.

    `None` means "the configured settings" -- exactly what run_scan() itself falls back
    to one frame down -- never a hardcoded default. A separate hardcoded default here
    once gave scan_service's run_scan_isolated(url) a 25-page budget for a crawl
    configured to fetch 100, aborting every large scan part-way through: the production
    path passes no settings at all, so that branch is not the fixture case it looked
    like. It would also pin the ceiling below the reaper threshold jobs/queue.py derives
    from the same property, putting back the drift that moved the formula here."""
    return (settings or get_settings()).scan_budget_seconds


#: How often the parent checks whether a scan running in the Windows child process was cancelled.
CANCEL_POLL_SECONDS = 2.0

#: Environment variable carrying the path of the cancel-signal file to the child process. The
#: child has no database access, so a cancel reaches it as the appearance of this file.
CANCEL_FILE_ENV = "CONSIVA_SCAN_CANCEL_FILE"


async def run_scan_isolated(
    root_url: str, settings: Settings | None = None, profile: ScanProfile | str | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
) -> ScanResult:
    """The entrypoint services should call instead of `run_scan()` directly.

    On Windows, spawns `run_single_scan.py` as a separate process so Playwright gets
    its own fresh (Proactor) event loop, decoupled from the parent process's
    SelectorEventLoop policy (set for the LangGraph checkpointer's psycopg — see
    app/jobs/worker.py and app/main.py). Uses plain `subprocess.run` in a thread
    executor, not `asyncio.create_subprocess_exec`: asyncio subprocess creation is
    itself unavailable under SelectorEventLoop on Windows, so the parent can't use
    asyncio's own subprocess APIs to launch the child either.

    On other platforms this conflict doesn't exist, so it just calls `run_scan()`
    directly — no subprocess overhead — but still under the SAME overall time budget
    the Windows subprocess gets.

    `profile` is a ScanProfile (or its name) from scan_profiles; with none given the
    historical deployment-wide budget applies. `should_cancel` is polled while the scan runs;
    when it returns True the crawl stops starting pages and returns what it has (see
    `_crawl_full_site`) -- the browser is never killed mid-page.
    """
    profile_obj = resolve_profile(profile, settings) if isinstance(profile, str) else profile
    budget = profile_obj.hard_timeout_seconds if profile_obj is not None else _scan_budget_seconds(settings)

    if sys.platform != "win32":
        # The budget is NOT optional here. Without it a scan that hangs inside
        # Playwright hangs the worker forever, and because jobs/worker.py runs
        # reap_stale_jobs() at the TOP of its loop, a hung job also blocks the very
        # mechanism that would recover it -- every subsequent scan then sits "queued"
        # indefinitely. Observed live: one job held the only worker for 15+ minutes
        # and five later scans never started, on every machine sharing the queue.
        # Windows was already bounded by subprocess.run(timeout=...); Linux was not,
        # which is why this only ever bit the deployed container.
        kwargs: dict = {}
        if profile_obj is not None:
            kwargs["profile"] = profile_obj
        if should_cancel is not None:
            kwargs["should_stop"] = should_cancel
        try:
            return await asyncio.wait_for(run_scan(root_url, settings, **kwargs), timeout=budget)
        except TimeoutError as exc:
            raise ScanTimeoutError(
                f"Scan of {root_url} exceeded its {budget:.0f}s budget and was aborted "
                "so the worker could continue with other jobs."
            ) from exc

    backend_dir = Path(__file__).resolve().parent.parent.parent
    cmd = [sys.executable, "-m", "app.scanner.run_single_scan", root_url]
    if profile_obj is not None:
        # Only the NAME crosses the process boundary. The child resolves it against the same
        # environment, so no page count or deadline is ever passed on a command line.
        cmd.append(profile_obj.name)

    cancel_dir = tempfile.mkdtemp(prefix="consiva-scan-")
    cancel_file = os.path.join(cancel_dir, "cancel")
    env = {**os.environ, CANCEL_FILE_ENV: cancel_file}

    def _run_subprocess() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
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
            timeout=budget,
            env=env,
        )

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, _run_subprocess)
    try:
        while True:
            done, _pending = await asyncio.wait({future}, timeout=CANCEL_POLL_SECONDS)
            if done:
                break
            if should_cancel is not None and not os.path.exists(cancel_file) and await should_cancel():
                # Signal, don't kill: the child sees the file between batches, stops starting
                # pages and returns its partial result normally.
                with open(cancel_file, "w", encoding="utf-8") as handle:
                    handle.write("cancel")
        completed = future.result()
    except subprocess.TimeoutExpired as exc:
        raise ScanTimeoutError(
            f"Scan of {root_url} exceeded its {budget:.0f}s budget and was aborted "
            "so the worker could continue with other jobs."
        ) from exc
    finally:
        try:
            if os.path.exists(cancel_file):
                os.remove(cancel_file)
            os.rmdir(cancel_dir)
        except OSError:
            pass

    if completed.returncode != 0:
        # A failure that repeating the scan cannot cure keeps its class across the process boundary, so the job queue does not retry it either.
        marker = "ScanNotPossible: "
        stderr = completed.stderr.strip()
        if marker in stderr:
            raise ScanNotPossible(stderr.rsplit(marker, 1)[1].splitlines()[0].strip(), "isolated")
        raise RuntimeError(f"Isolated scan process failed for {root_url}: {stderr}")

    return ScanResult.model_validate_json(completed.stdout)
