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
    ".cc-revoke", "[data-cc='c-settings']", "#cookiescript_badge", ".osano-cm-widget", "#CookiebotWidget", ".didomi-notice-reopen", "[data-consiva-consent-settings]", ".cmp-floating-btn",
)
#: Words on a control inside the reopened dialog that takes consent back. "Save" or "confirm" alone does not: it keeps whatever the switches say.
WITHDRAW_HINTS = (
    "withdraw consent", "withdraw", "revoke", "reject all", "decline all", "deny all", "reject non-essential", "reject optional", "necessary only", "only necessary", "essential only",
    "only essential", "turn off all", "opt out", "reject", "decline",
)
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


#: Finds the clickable element whose words (text, aria-label, title, value) match the earliest hint, looking into open shadow roots too, skips what is not visible, and with `stay on page` skips a link that
#: leaves the page. It stamps the winner with data-consiva-pick so that it can be clicked, and returns {hint, text} (or null).
_PICK_JS = """([hints, stay]) => {
  const sel = "button, a, [role='button'], input[type='button'], input[type='submit'], [onclick]";
  const all = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll(sel)) all.push(el);
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  let best = null;
  for (const el of all) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!(r.width > 0 && r.height > 0) || cs.visibility === 'hidden' || cs.display === 'none') continue;
    const text = [(el.innerText || '').trim(), el.getAttribute('aria-label') || '', el.getAttribute('title') || '', el.getAttribute('value') || ''].join(' ').trim().toLowerCase();
    if (!text) continue;
    const idx = hints.findIndex((h) => text.includes(h));
    if (idx < 0 || (best && idx >= best.hint)) continue;
    if (stay && el.tagName === 'A') { const href = (el.getAttribute('href') || '').trim().toLowerCase(); if (href && !href.startsWith('#') && !href.startsWith('javascript:')) continue; }
    best = { hint: idx, el, text: text.slice(0, 80) };
  }
  if (!best) return null;
  best.el.setAttribute('data-consiva-pick', '1');
  return { hint: best.hint, text: best.text };
}"""
_UNPICK_JS = """() => {
  const walk = (root) => { for (const el of root.querySelectorAll('[data-consiva-pick]')) el.removeAttribute('data-consiva-pick'); for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot); };
  walk(document);
}"""


async def _click_hint(page: Page, hints: tuple[str, ...], *, selectors: tuple[str, ...] = (), stay_on_page: bool = False) -> str | None:
    """Clicks the first visible control whose words match a hint (in hint order) and returns what it said, or None. With `stay_on_page`, a link that goes to another page is passed over:
    a reopen control changes the dialog, not the address."""
    frames, _ = await _probe_frames(page)
    for frame in frames:
        for selector in selectors:
            locator = frame.locator(selector)
            try:
                if await _bounded(locator.count()) and await _bounded(locator.first.is_visible()):
                    label = ((await locator.first.inner_text(timeout=1000)).strip() or await locator.first.get_attribute("aria-label") or await locator.first.get_attribute("title") or selector)[:80]
                    await locator.first.click(timeout=3000)
                    return label
            except PlaywrightError:
                continue
    # One pass per frame: every clickable element, in the light DOM and in open shadow roots (a consent tool's own widget lives there), is read once, its words are matched against the hints
    # in hint order, and only the winner is clicked. (Before, each hint made a browser call per element, capped at the first 120 on the page: a long page took minutes, and a control at the
    # end of the page, in a shadow root, was never reached.)
    best: tuple[int, int, str] | None = None            # (hint index, frame index, text)
    for fi, frame in enumerate(frames):
        try:
            found = await _bounded(frame.evaluate(_PICK_JS, [list(hints), stay_on_page]))
        except PlaywrightError:
            continue
        if found and (best is None or found["hint"] < best[0]):
            best = (found["hint"], fi, found["text"])
    try:
        if best is not None:
            try:
                await frames[best[1]].locator('[data-consiva-pick="1"]').first.click(timeout=3000)
                return best[2]
            except PlaywrightError:
                return None
    finally:
        for frame in frames:
            try:
                await _bounded(frame.evaluate(_UNPICK_JS))
            except PlaywrightError:
                pass
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
        # Watched for twice the usual settle time: an analytics tag batches its hits and may send one several seconds after the click (Google Analytics on swaransoft.com did so in five scans
        # of six and fell outside a 2.5 s window in the sixth, which made one site score 91 or 97). A longer look removes that chance miss; it does not change what counts.
        await page.wait_for_timeout(settle_ms * 2)
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
