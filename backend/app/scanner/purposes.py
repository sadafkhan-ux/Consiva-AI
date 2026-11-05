"""Where a consent tool offers choices by purpose, does each choice do what it says? (Functional Scope DS-12; readiness indicator I9; Deep Scan only.)

For every purpose the preferences dialog lets a visitor switch on (analytics, marketing, functional and so on), one fresh browser session: open the preferences, switch every optional purpose off, switch
that one purpose on, save, and record which analytics or advertising services are then contacted. Purposes are tested independently, each in its own session, so one cannot colour another.

"As expected" means nothing outside the chosen purpose was contacted: switching on analytics should contact analytics services and no advertising service; switching on marketing should contact advertising
services and no analytics service; switching on a functional purpose should contact neither. The verdict follows the evidence and nothing else. Every way the test can stop short has its own reason in plain
words (DS-15): the banner offered no choice by purpose, the preferences could not be opened, no save control was found. A test that did not finish is "not tested", never "failed".

Requests are classified with the same catalogue as the rest of the scan (scanner/withdrawal.non_essential_requests). Cookies are not read: their classification needs the lookup table the scanner does not hold.
The result goes into the consent signal's evidence under "purposes"; scoring/domain_score.py reads it from there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.scanner.consent_interactor import _bounded, _probe_frames, pass_consent_gate
from app.scanner.withdrawal import _click_hint, _watch, non_essential_requests

logger = logging.getLogger(__name__)

#: Words on the control that opens the preferences, most specific first. A catalogued consent tool's own button is tried before the words.
PREFS_HINTS = ("customize", "customise", "manage preferences", "manage cookies", "manage consent", "cookie settings", "cookie preferences", "preferences", "more options", "settings", "options", "choose")
PREFS_SELECTORS = (
    "#onetrust-pc-btn-handler", ".cky-btn-customize", "#CybotCookiebotDialogBodyLevelButtonCustomize", ".cmplz-manage-options", ".iubenda-cs-customize-btn", "#didomi-notice-learn-more-button",
    "[data-consiva-consent-preferences]",
)
#: Words on the control that saves the chosen purposes. "Reject" and "accept all" are not here: they would decide every purpose at once.
SAVE_HINTS = (
    "save my choices", "save preferences", "save settings", "confirm my choices", "confirm choices", "allow selection", "accept selected", "accept selection", "save and close", "save & close",
    "save", "confirm",
)
_TOGGLE = "input[type='checkbox'], [role='switch'], [role='checkbox']"
_SETTLE_MS = 3000
_MAX_PURPOSES = 4

_NECESSARY = re.compile(r"necessary|essential|strictly|required|always active|always on", re.IGNORECASE)
_KINDS = (
    ("analytics", re.compile(r"analytic|statistic|performance|measurement|insight", re.IGNORECASE)),
    ("marketing", re.compile(r"market|advertis|targeting|retarget|personali[sz]ed ad|promotion", re.IGNORECASE)),
    ("functional", re.compile(r"function|preference|personali[sz]ation|comfort|remember", re.IGNORECASE)),
)
#: Which analytics or advertising categories a purpose may contact once it is switched on.
ALLOWED = {"analytics": {"analytics"}, "marketing": {"marketing"}, "functional": set(), "other": set()}

REASONS = {
    "homepage_not_loaded": "The home page did not load, so choices by purpose were not tested.",
    "no_preferences_control": "No control that opens the cookie preferences was found, so choices by purpose were not tested.",
    "no_purpose_choices": "The consent tool offers no choice by purpose (only accept or reject), so there is nothing to test purpose by purpose.",
    "no_save_control": "The preferences offered no control that saves the chosen purposes, so choices by purpose were not tested.",
    "error": "The test of choices by purpose could not be completed.",
}


def kind_of(label: str) -> str:
    """The purpose a switch's label names: analytics, marketing, functional, or other when the words do not say."""
    for kind, pattern in _KINDS:
        if pattern.search(label or ""):
            return kind
    return "other"


def is_necessary(label: str) -> bool:
    return bool(_NECESSARY.search(label or ""))


def verdict(kind: str, observed: list[dict]) -> str:
    """"as_expected" when nothing outside the purpose that was switched on was contacted, else "unexpected"."""
    stray = [o for o in observed if o.get("category") not in ALLOWED.get(kind, set())]
    return "unexpected" if stray else "as_expected"


async def _label_of(el) -> str:
    """What a switch is called: its label, accessible name, or the text of the small row that holds it."""
    parts: list[str] = []
    try:
        parts.append(await el.get_attribute("aria-label") or "")
        ident = await el.get_attribute("id")
        if ident:
            parts.append(await el.evaluate("(e, i) => { const l = document.querySelector(`label[for=\"${i}\"]`); return l ? l.innerText : ''; }", ident))
        parts.append(await el.evaluate("e => { const l = e.closest('label'); return l ? l.innerText : ''; }"))
        if not any(p.strip() for p in parts):
            parts.append(await el.evaluate("e => { let n = e.parentElement; for (let i = 0; i < 3 && n; i++, n = n.parentElement) { const t = (n.innerText || '').trim(); if (t && t.length < 160) return t; } return ''; }"))
    except PlaywrightError:
        pass
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()[:120]


async def _toggles(page: Page) -> list[dict]:
    """The switches in the open preferences: [{frame, el, label, kind, locked, checked}] in page order."""
    out: list[dict] = []
    frames, _ = await _probe_frames(page)
    for frame in frames:
        loc = frame.locator(_TOGGLE)
        try:
            n = min(await _bounded(loc.count()), 30)
        except PlaywrightError:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                disabled = (await el.get_attribute("disabled")) is not None or (await el.get_attribute("aria-disabled")) == "true"
                label = await _label_of(el)
                checked = (await el.is_checked()) if (await el.evaluate("e => e.tagName")) == "INPUT" else (await el.get_attribute("aria-checked")) == "true"
            except PlaywrightError:
                continue
            if not label:
                continue
            out.append({"el": el, "label": label, "kind": kind_of(label), "locked": disabled or is_necessary(label), "checked": checked})
    return out


async def _set(t: dict, on: bool) -> bool:
    """Puts a switch in the wanted state (a custom switch often hides its input, so the click may need to go to the label) and reports whether it took."""
    el = t["el"]
    try:
        current = (await el.is_checked()) if (await el.evaluate("e => e.tagName")) == "INPUT" else (await el.get_attribute("aria-checked")) == "true"
        if current == on:
            return True
        try:
            await el.click(timeout=2000)
        except PlaywrightError:
            await el.click(timeout=2000, force=True)
        now = (await el.is_checked()) if (await el.evaluate("e => e.tagName")) == "INPUT" else (await el.get_attribute("aria-checked")) == "true"
        return now == on
    except PlaywrightError:
        return False


async def _open_preferences(page: Page) -> bool:
    await pass_consent_gate(page)
    opened = await _click_hint(page, PREFS_HINTS, selectors=PREFS_SELECTORS, stay_on_page=True)
    if opened is None:
        await page.wait_for_timeout(800)
        opened = await _click_hint(page, PREFS_HINTS, selectors=PREFS_SELECTORS, stay_on_page=True)
    if opened is None:
        return False
    await page.wait_for_timeout(1000)
    return True


async def run_purpose_test(make_context: Callable[[], Awaitable], root_url: str, *, goto: Callable[[Page, str], Awaitable[bool]], settle_ms: int = _SETTLE_MS) -> dict:
    """One fresh session to find the purposes the preferences offer, then one per purpose. `make_context()` returns a new browser session with the address guard on.

    Returns {"tested": bool, "reason": key of REASONS or None, "purposes": [{kind, label, observed: [{vendor, category, host}], verdict}], "requests_only": True}. `tested` is true once at least one
    purpose was switched on alone and watched; each purpose carries its own verdict.
    """
    site = urlparse(root_url).netloc
    result: dict = {"tested": False, "reason": None, "purposes": [], "requests_only": True}

    async def session(target: str | None) -> dict | str:
        """Opens the preferences in a fresh session. With target=None it lists the optional purposes; otherwise it switches only `target` on, saves, and returns what was contacted."""
        context = await make_context()
        try:
            page = await context.new_page()
            records: list[tuple[str, str]] = []
            await _watch(page, records)
            if not await goto(page, root_url):
                return "homepage_not_loaded"
            try:
                await page.wait_for_load_state("load", timeout=15_000)
            except PlaywrightError:
                pass
            await page.wait_for_timeout(settle_ms)
            if not await _open_preferences(page):
                return "no_preferences_control"
            toggles = [t for t in await _toggles(page) if not t["locked"]]
            if target is None:
                return {"found": [{"kind": t["kind"], "label": t["label"]} for t in toggles]}
            if not toggles:
                return "no_purpose_choices"
            for t in toggles:
                await _set(t, t["label"] == target)
            # What the save causes starts at the click, so the mark is taken before it (the opposite of the withdrawal test, which must not count what was already in flight).
            mark = len(records)
            saved = await _click_hint(page, SAVE_HINTS)
            if saved is None:
                return "no_save_control"
            await page.wait_for_timeout(settle_ms)
            return {"observed": non_essential_requests(records[mark:], site), "saved_with": saved}
        finally:
            try:
                await context.close()
            except PlaywrightError:
                pass

    try:
        listing = await session(None)
        if isinstance(listing, str):
            return {**result, "reason": listing}
        found = listing["found"]
        if not found:
            return {**result, "reason": "no_purpose_choices"}
        seen: set[str] = set()
        for choice in found:
            if choice["label"] in seen or len(result["purposes"]) >= _MAX_PURPOSES:
                continue
            seen.add(choice["label"])
            one = await session(choice["label"])
            if isinstance(one, str):
                result["reason"] = one
                continue
            result["purposes"].append({"kind": choice["kind"], "label": choice["label"], "observed": one["observed"], "verdict": verdict(choice["kind"], one["observed"])})
        result["tested"] = bool(result["purposes"])
        if result["tested"]:
            result["reason"] = None
        return result
    except Exception as exc:  # noqa: BLE001 — best effort: a failed test is recorded as not tested, never fatal to the scan
        logger.warning("The test of choices by purpose for %s did not complete: %s", root_url, exc)
        return {**result, "tested": False, "reason": "error"}
