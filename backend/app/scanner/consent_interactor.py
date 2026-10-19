"""Finds and clicks a consent banner's Accept/Reject control, for the three-pass
consent-state scan (master prompt §5, Build Plan Component 2). Tries known CMPs'
selectors first (more reliable when they match), then falls back to a generic
text-match search over clickable elements. Never raises -- returns one of
"clicked" | "click_failed" | "not_found" so the caller can record the real outcome
explicitly rather than guess, and so "we found a control but couldn't click it"
(detached/covered by an overlay) is never conflated with "there was nothing here at
all" (scanner-hardening audit, section 8).

Searches EVERY frame on the page, not just the top-level document: many real CMPs
(Quantcast Choice/IAB TCF vendors especially) render their banner inside an iframe,
often cross-origin. Playwright drives frames at the browser-automation protocol
level, so `frame.locator(...)` works against a cross-origin iframe the same way it
does against the main frame — this isn't limited by same-origin JS restrictions the
way a page's own script would be.
"""

import asyncio
import logging

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page

from app.rules.tracker_catalog import CMP_CATALOG

logger = logging.getLogger(__name__)

_ACCEPT_HINTS = ("accept all", "allow all", "accept cookies", "i agree", "agree", "accept")
_REJECT_HINTS = ("reject all", "decline all", "reject non-essential", "deny all", "reject", "decline")
_MAX_CANDIDATES = 50
_MAX_FRAMES = 20  # bounds a pathological many-iframe (ad-heavy) page from ballooning search time
_CLICKABLE_SELECTOR = "button, a[role='button'], [role='button'], input[type='button'], input[type='submit']"


#: Seconds a child frame gets to answer a trivial script before it is treated as unresponsive, and
#: seconds one locator query may take. Playwright's Locator.count() and Frame.content() have no timeout
#: of their own; on a real site (smartians.ai) one child frame never answered, and every query to it
#: waited until the whole-scan timeout killed the scan. These are per-call safeguards, not scan limits.
_FRAME_PROBE_TIMEOUT_SECONDS = 3
_FRAME_OP_TIMEOUT_SECONDS = 10


async def _bounded(awaitable):
    """`awaitable`, or a PlaywrightError if it does not finish in time -- which every caller here
    already handles as "this frame is not usable", so a hung frame costs seconds, not the scan."""
    try:
        return await asyncio.wait_for(awaitable, timeout=_FRAME_OP_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise PlaywrightError(f"the frame did not respond within {_FRAME_OP_TIMEOUT_SECONDS}s") from exc


async def _probe_frames(page: Page) -> tuple[list[Page | Frame], list[Frame]]:
    """(frames that answer, frames that do not). The first frame is the page itself and is always
    searched; every child frame must answer a trivial script within a few seconds. They are probed
    together, so any number of hung frames costs one timeout, not one each."""
    frames = _frames_to_search(page)
    children = frames[1:]

    async def answers(frame) -> bool:
        try:
            await asyncio.wait_for(frame.evaluate("1"), timeout=_FRAME_PROBE_TIMEOUT_SECONDS)
            return True
        except (TimeoutError, PlaywrightError) as exc:
            logger.warning("Frame %r is not responding and is skipped: %s", getattr(frame, "url", ""), type(exc).__name__)
            return False

    verdicts = await asyncio.gather(*(answers(f) for f in children)) if children else []
    responsive = frames[:1] + [f for f, ok in zip(children, verdicts) if ok]
    unresponsive = [f for f, ok in zip(children, verdicts) if not ok]
    return responsive, unresponsive


def _frames_to_search(page: Page) -> list[Page | Frame]:
    """`page.frames` includes the main frame first, then every nested iframe attached
    right now (consent banners are typically injected early, so this snapshot at
    call-time is normally sufficient — a banner injected into a NEW iframe after this
    call started wouldn't be caught, a narrower version of the same "can't prove a
    negative from one instant" limitation the rest of this module already accepts)."""
    return list(page.frames[:_MAX_FRAMES])


_GATE_CLICK_TIMEOUT_MS = 3000
_GATE_BANNER_WAIT_MS = 8000


async def pass_consent_gate(page: Page) -> dict | None:
    """Answers a gate a CMP shows before its consent choice (Consiva's age gate), if one is on the page.

    Returns None when there is no gate, else a record of what was done --
    {"kind", "cmp", "answer": "adult", "outcome"} with outcome "passed" (the consent banner then appeared),
    "click_failed", or "banner_not_shown_after_gate". The scanner answers as an adult visitor would; that
    is an assertion the test visitor makes, so it is RECORDED in the scan evidence and never done silently.
    Nothing else about the page is touched, and a gate that is not there costs one bounded probe.
    """
    responsive, _ = await _probe_frames(page)
    for frame in responsive:
        for cmp in CMP_CATALOG:
            if not cmp.gate_selector:
                continue
            locator = frame.locator(cmp.gate_selector)
            try:
                if await _bounded(locator.count()) == 0 or not await _bounded(locator.first.is_visible()):
                    continue
            except PlaywrightError:
                continue
            record = {"kind": cmp.gate_kind, "cmp": cmp.cmp_vendor, "answer": "adult"}
            try:
                await locator.first.click(timeout=_GATE_CLICK_TIMEOUT_MS)
            except PlaywrightError as exc:
                logger.debug("Gate control %r matched but click failed: %s", cmp.gate_selector, exc)
                return {**record, "outcome": "click_failed"}
            target = cmp.accept_selector or cmp.reject_selector
            if target:
                try:
                    await frame.locator(target).first.wait_for(state="visible", timeout=_GATE_BANNER_WAIT_MS)
                except PlaywrightError:
                    return {**record, "outcome": "banner_not_shown_after_gate"}
            return {**record, "outcome": "passed"}
    return None


async def _try_cmp_selectors(page: Page, *, accept: bool) -> str:
    """Returns "clicked" | "click_failed" | "not_found". "click_failed" is distinct
    from "not_found": a real, matching element existed (count() > 0) but the click
    itself raised (detached, covered by an overlay, not interactable) -- that is a
    genuinely different, more informative outcome than "there was nothing here to
    click" and must not be reported the same way (scanner-hardening audit, section 8)."""
    found_but_failed = False
    for frame in (await _probe_frames(page))[0]:
        for cmp in CMP_CATALOG:
            selector = cmp.accept_selector if accept else cmp.reject_selector
            if not selector:
                continue
            locator = frame.locator(selector)
            try:
                if await _bounded(locator.count()) > 0:
                    try:
                        await locator.first.click(timeout=3000)
                        return "clicked"
                    except PlaywrightError as exc:
                        logger.debug("CMP selector %r matched but click failed in frame %r: %s", selector, frame.url, exc)
                        found_but_failed = True
            except PlaywrightError as exc:  # a missing/unusable selector just means "try the next CMP"
                logger.debug("CMP selector %r not usable in frame %r: %s", selector, frame.url, exc)
    return "click_failed" if found_but_failed else "not_found"


async def _try_text_match(page: Page, *, hints: tuple[str, ...]) -> str:
    """Returns "clicked" | "click_failed" | "not_found" -- same distinction as
    _try_cmp_selectors above, for the generic text-match fallback path."""
    found_but_failed = False
    for frame in (await _probe_frames(page))[0]:
        candidates = frame.locator(_CLICKABLE_SELECTOR)
        try:
            count = await _bounded(candidates.count())
        except PlaywrightError as exc:
            logger.debug("Could not enumerate clickable elements in frame %r: %s", frame.url, exc)
            continue

        for i in range(min(count, _MAX_CANDIDATES)):
            element = candidates.nth(i)
            try:
                text = (await element.inner_text(timeout=1000)).strip().lower()
            except PlaywrightError as exc:  # detached/hidden elements are expected on real pages
                logger.debug("Could not read candidate #%d text in frame %r: %s", i, frame.url, exc)
                continue
            if any(hint in text for hint in hints):
                try:
                    await element.click(timeout=3000)
                    return "clicked"
                except PlaywrightError as exc:
                    logger.debug("Matched candidate #%d in frame %r but click failed: %s", i, frame.url, exc)
                    found_but_failed = True
    return "click_failed" if found_but_failed else "not_found"


# A CMP iframe attached asynchronously (common for some ad-network-hosted IAB TCF
# vendors, which inject their consent iframe via a JS call shortly after page load
# rather than having it present in the initial DOM) can miss the single frame-list
# snapshot _frames_to_search() takes. One bounded retry after a short wait catches
# that case; this cost is only ever paid when the first pass found nothing, so a page
# with a normally-present banner is unaffected.
_RETRY_WAIT_MS = 800

# Outcome precedence when combining multiple search attempts (CMP selectors, text
# match, the retry pass): a real click anywhere wins outright; failing that, "we found
# something but couldn't click it" is more informative than "nothing was there at all"
# and must not be silently downgraded to "not_found" just because a LATER attempt also
# found nothing.
_STATUS_RANK = {"clicked": 2, "click_failed": 1, "not_found": 0}


def _best(*statuses: str) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK[s])


async def _search_all_frames(page: Page, *, hints: tuple[str, ...], accept: bool) -> str:
    """Returns "clicked" | "click_failed" | "not_found" -- see _try_cmp_selectors'
    docstring for why "click_failed" is kept distinct rather than collapsed into
    "not_found"."""
    first_pass = _best(await _try_cmp_selectors(page, accept=accept), await _try_text_match(page, hints=hints))
    if first_pass == "clicked":
        return first_pass
    await page.wait_for_timeout(_RETRY_WAIT_MS)
    second_pass = _best(await _try_cmp_selectors(page, accept=accept), await _try_text_match(page, hints=hints))
    return _best(first_pass, second_pass)


async def click_accept(page: Page) -> str:
    return await _search_all_frames(page, hints=_ACCEPT_HINTS, accept=True)


async def click_reject(page: Page) -> str:
    return await _search_all_frames(page, hints=_REJECT_HINTS, accept=False)


async def confirm_interaction_completed(page: Page, *, accept: bool) -> bool:
    """Whether a "clicked" Accept/Reject actually took effect, observed independently
    of the click.

    "clicked" only means Playwright dispatched a click that did not raise. A "Decline"
    link in an unrelated modal satisfies that, and so does a CMP that answers Reject by
    opening a preferences panel instead of recording a choice -- neither establishes
    the consent state the post_accept/post_reject evidence is then filed under. A banner
    that has recorded a decision dismisses itself, so the observation is: no control
    for that side (catalogued CMP selector, or clickable text matching the same hints
    the click used) is still visible in any frame.

    Conservative in the safe direction: any frame that cannot be inspected makes this
    False. "Unconfirmed" suppresses a post-reject finding; it never asserts one.
    """
    hints = _ACCEPT_HINTS if accept else _REJECT_HINTS
    try:
        responsive, unresponsive = await _probe_frames(page)
        if unresponsive:
            # A frame that will not answer might be the one still showing the banner. Skipping it
            # would let "confirmed" be claimed without having looked there, so it is unconfirmed.
            logger.debug("Cannot confirm the consent interaction: %d frame(s) not responding", len(unresponsive))
            return False
        for frame in responsive:
            for cmp in CMP_CATALOG:
                selector = cmp.accept_selector if accept else cmp.reject_selector
                if not selector:
                    continue
                locator = frame.locator(selector)
                if await _bounded(locator.count()) > 0 and await _bounded(locator.first.is_visible()):
                    return False

            candidates = frame.locator(_CLICKABLE_SELECTOR)
            count = await _bounded(candidates.count())
            for i in range(min(count, _MAX_CANDIDATES)):
                element = candidates.nth(i)
                if not await _bounded(element.is_visible()):
                    continue
                text = (await element.inner_text(timeout=1000)).strip().lower()
                if any(hint in text for hint in hints):
                    return False
    except PlaywrightError as exc:
        logger.debug("Could not confirm the consent interaction took effect: %s", exc)
        return False
    return True
