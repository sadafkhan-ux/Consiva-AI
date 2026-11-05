"""Can a consent choice be taken back, and is taking it back honoured? (Functional Scope DS-10, DS-11, DS-15; Scanner Score B5 and B6; Deep Scan only.)

In one fresh browser session: Accept, then find the control that reopens the cookie choice, withdraw from the reopened dialog, and watch what the page does next. Then open up to two further
pages in the same session and watch those too, because a choice forgotten on the next page is not a choice.

What is recorded is what was observed and nothing more. Every way the test can stop short has its own reason, in words a site owner can act on (DS-15): no option to reopen the choice was
found, the dialog offered no way to withdraw, Accept could not be clicked. A test that did not finish is "not tested", never "failed". The result goes into the consent signal's evidence
under "withdrawal"; the scoring (scoring/domain_score.py) reads it from there.

Requests are classified by the same catalogue as the rest of the scan, so "analytics or advertising" means the same thing here as in the three consent states. Cookies are not read:
their classification needs the lookup table, which the scanner does not hold, so the test reports requests only and says so.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.rules.consent_rules import classify_tracker
from app.scanner.consent_interactor import (
    _bounded,
    _probe_frames,
    click_accept,
    confirm_interaction_completed,
    pass_consent_gate,
)
from app.scanner.tracker_detector import detect_trackers

logger = logging.getLogger(__name__)

#: Words on a control that reopens the cookie choice, most specific first. A bare "cookies" is not here: it is as often a link to the cookie policy, which leaves the page.
REOPEN_HINTS = (
    "cookie settings", "cookie preferences", "cookie preference", "cookie choices", "manage cookies", "manage cookie", "manage preferences", "manage consent", "consent settings",
    "consent preferences", "privacy settings", "privacy preferences", "your privacy choices", "privacy choices", "change consent", "change cookie", "update cookie", "update consent",
    "customize cookies", "customise cookies", "cookie consent", "do not sell or share",
)
#: Selectors of the persistent "reopen" control some consent tools draw themselves (a floating icon, a footer link). Tried before the words above.
REOPEN_SELECTORS = (
    ".cky-btn-revisit", ".cky-btn-revisit-wrapper", "#ot-sdk-btn", ".ot-sdk-show-settings", ".termly-display-preferences", ".iubenda-cs-preferences-link", ".cmplz-manage-consent",
    ".cc-revoke", "[data-cc='c-settings']", "#cookiescript_badge", ".osano-cm-widget", "#CookiebotWidget", ".didomi-notice-reopen", "[data-consiva-consent-settings]",
)
#: Words on a control inside the reopened dialog that takes consent back. "Save" or "confirm" alone does not: it keeps whatever the switches say.
WITHDRAW_HINTS = (
    "withdraw consent", "withdraw", "revoke", "reject all", "decline all", "deny all", "reject non-essential", "reject optional", "necessary only", "only necessary", "essential only",
    "only essential", "turn off all", "opt out", "reject", "decline",
)
_CLICKABLE = "button, a, [role='button'], input[type='button'], input[type='submit'], [onclick]"
_MAX_CANDIDATES = 120
_LOADER = re.compile(r"googletagmanager\.com/(gtm\.js|gtag/js)", re.IGNORECASE)
_SETTLE_MS = 2500
_FURTHER_PAGES = 2
_GRACE_MS = 500

#: Why the test stopped short, in the words shown to a site owner.
REASONS = {
    "homepage_not_loaded": "The home page did not load, so withdrawal was not tested.",
    "accept_not_clicked": "Accept could not be clicked, so there was no choice to withdraw.",
    "no_reopen_control": "No option to reopen or manage the cookie choice was found, so withdrawal could not be tested.",
    "no_withdraw_control": "The reopened cookie dialog offered no control that withdraws consent (such as Reject all), so withdrawal could not be tested.",
    "error": "The withdrawal test could not be completed.",
}


def non_essential_requests(records: list[tuple[str, str]], site_domain: str) -> list[dict]:
    """The analytics and advertising services contacted in `records` ((address, resource type) pairs): [{vendor, category, host}]. The tag loader itself is not counted: it is the container, not the tracking."""
    trackers = detect_trackers(site_domain=site_domain, scripts_by_page={}, network_requests_by_page={"page": list(records)})
    out, seen = [], set()
    for t in trackers:
        c = classify_tracker(t)
        if c.category not in ("analytics", "marketing") or _LOADER.search(c.script_src or ""):
            continue
        host = (urlparse(c.script_src).hostname or "")[:120]
        key = (c.vendor, host)
        if key not in seen:
            seen.add(key)
            out.append({"vendor": c.vendor, "category": c.category, "host": host})
    return out


async def _click_hint(page: Page, hints: tuple[str, ...], *, selectors: tuple[str, ...] = (), stay_on_page: bool = False) -> str | None:
    """Clicks the first visible control whose words match a hint (in hint order) and returns what it said, or None. With `stay_on_page`, a link that goes to another page is passed over:
    a reopen control changes the dialog, not the address."""
    frames, _ = await _probe_frames(page)
    for frame in frames:
        for selector in selectors:
            locator = frame.locator(selector)
            try:
                if await _bounded(locator.count()) and await _bounded(locator.first.is_visible()):
                    label = (await locator.first.inner_text(timeout=1000)).strip()[:80] or selector
                    await locator.first.click(timeout=3000)
                    return label
            except PlaywrightError:
                continue
    for hint in hints:
        for frame in frames:
            candidates = frame.locator(_CLICKABLE)
            try:
                count = min(await _bounded(candidates.count()), _MAX_CANDIDATES)
            except PlaywrightError:
                continue
            for i in range(count):
                el = candidates.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    text = " ".join(filter(None, [(await el.inner_text(timeout=800)).strip(), await el.get_attribute("aria-label") or "", await el.get_attribute("title") or "",
                                                  await el.get_attribute("value") or ""])).strip().lower()
                    if hint not in text:
                        continue
                    if stay_on_page and (await el.evaluate("e => e.tagName")) == "A":
                        href = (await el.get_attribute("href") or "").strip().lower()
                        if href and not href.startswith(("#", "javascript:")):
                            continue
                    await el.click(timeout=3000)
                    return text[:80]
                except PlaywrightError:
                    continue
    return None


async def _watch(page: Page, records: list[tuple[str, str]]) -> None:
    page.on("request", lambda req: records.append((req.url, req.resource_type)))


async def run_withdrawal_test(context, root_url: str, further_urls: list[str], *, goto: Callable[[Page, str], Awaitable[bool]], settle_ms: int = _SETTLE_MS) -> dict:
    """The withdrawal test in `context` (a fresh browser session the caller has made, with its address guard on). `goto(page, url)` loads a page and says whether it loaded.

    Returns {"tested": bool, "reason": key of REASONS or None, "reopen_control", "withdraw_control", "accepted_non_essential": [...], "after_withdrawal": [...], "further_pages": [{url, loaded,
    non_essential: [...]}], "requests_only": True}. `tested` is true once the withdraw control was clicked; "honoured" is then "no analytics or advertising request after it".
    """
    site = urlparse(root_url).netloc
    result: dict = {"tested": False, "reason": None, "reopen_control": None, "withdraw_control": None, "accepted_non_essential": [], "after_withdrawal": [], "further_pages": [], "requests_only": True}
    page = await context.new_page()
    records: list[tuple[str, str]] = []
    await _watch(page, records)
    try:
        if not await goto(page, root_url):
            return {**result, "reason": "homepage_not_loaded"}
        try:
            await page.wait_for_load_state("load", timeout=15_000)
        except PlaywrightError:
            pass
        await page.wait_for_timeout(settle_ms)
        await pass_consent_gate(page)
        if await click_accept(page) != "clicked":
            return {**result, "reason": "accept_not_clicked"}
        await page.wait_for_timeout(2000)
        await confirm_interaction_completed(page, accept=True)
        result["accepted_non_essential"] = non_essential_requests(records, site)

        reopened = await _click_hint(page, REOPEN_HINTS, selectors=REOPEN_SELECTORS, stay_on_page=True)
        if reopened is None:
            await page.wait_for_timeout(800)
            reopened = await _click_hint(page, REOPEN_HINTS, selectors=REOPEN_SELECTORS, stay_on_page=True)
        if reopened is None:
            return {**result, "reason": "no_reopen_control"}
        result["reopen_control"] = reopened
        await page.wait_for_timeout(1000)

        withdrawn = await _click_hint(page, WITHDRAW_HINTS)
        if withdrawn is None:
            return {**result, "reason": "no_withdraw_control"}
        result["withdraw_control"] = withdrawn
        # A request already on its way when the click landed is the old choice's, not the new one's: what counts starts after a short grace, taken once the click has returned.
        await page.wait_for_timeout(_GRACE_MS)
        mark = len(records)
        await page.wait_for_timeout(settle_ms)
        result["after_withdrawal"] = non_essential_requests(records[mark:], site)
        result["tested"] = True

        for url in further_urls[:_FURTHER_PAGES]:
            extra: list[tuple[str, str]] = []
            further = await context.new_page()
            await _watch(further, extra)
            try:
                loaded = await goto(further, url)
                if loaded:
                    await further.wait_for_timeout(settle_ms)
                result["further_pages"].append({"url": url[:300], "loaded": bool(loaded), "non_essential": non_essential_requests(extra, site) if loaded else []})
            finally:
                if not further.is_closed():
                    await further.close()
        return result
    except Exception as exc:  # noqa: BLE001 — best effort: a failed test is recorded as not tested, never fatal to the scan
        logger.warning("The withdrawal test for %s did not complete: %s", root_url, exc)
        return {**result, "tested": False, "reason": "error"}
    finally:
        if not page.is_closed():
            await page.close()
