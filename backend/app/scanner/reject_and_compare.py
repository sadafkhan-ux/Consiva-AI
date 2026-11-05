"""Two more observations the scorecard asks for about the consent banner.

1. Is Reject offered with the same ease as Accept? (Scorecard P2.2; Act s.6(1), s.6(4); Rules r.3(c)(i).) The Accept and Reject controls on the first layer are measured: size, font size, whether each is a
   filled button or a bare link, and its background. Reject is "less prominent" only by a stated rule below; the observation records the numbers, so a person can see what was measured.
2. Does Reject hold on further pages? (Scorecard P4.3.) In one fresh browser session: Reject, then up to two further pages in the same session, watching for analytics or advertising requests.
   Deep Scan only. A test that could not run to the end is "not tested" with a reason, never "failed"; requests are classified with the same catalogue as the rest of the scan.

The results go into the consent signal's evidence under "button_comparison" and "reject_persistence"; scoring/scorecard.py reads them from there.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.scanner.consent_interactor import (
    click_reject,
    confirm_interaction_completed,
    pass_consent_gate,
)
from app.scanner.withdrawal import _watch, non_essential_requests

logger = logging.getLogger(__name__)

ACCEPT_WORDS = ("accept all", "allow all", "accept cookies", "i agree", "accept")
REJECT_WORDS = ("reject all", "decline all", "deny all", "reject non-essential", "only necessary", "necessary only", "reject", "decline", "deny")
#: Reject is less prominent when it is smaller than this share of Accept, its text is smaller than this share of Accept's, or it is a bare link beside a filled button.
MIN_AREA_SHARE = 0.5
MIN_FONT_SHARE = 0.8

_MEASURE_JS = """(words) => {
  const [accept, reject] = words;
  const els = Array.from(document.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']"));
  const info = (el) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
    const text = ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim().toLowerCase();
    const bg = cs.backgroundColor, transparent = !bg || bg === 'transparent' || /rgba\\(\\s*\\d+,\\s*\\d+,\\s*\\d+,\\s*0\\s*\\)/.test(bg);
    const border = parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none';
    return { visible, text, tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height), font: parseFloat(cs.fontSize) || 0, filled: !transparent, bordered: border, top: Math.round(r.top) };
  };
  const find = (list) => { for (const w of list) { for (const el of els) { const i = info(el); if (i.visible && i.text && i.text.length < 60 && i.text.includes(w)) return i; } } return null; };
  return { accept: find(accept), reject: find(reject) };
}"""


def judge(accept: dict, reject: dict) -> tuple[bool, str]:
    """(comparable, why). Reject is less prominent than Accept by a size, a text size, or being a bare link where Accept is a filled button."""
    a_area, r_area = accept["w"] * accept["h"], reject["w"] * reject["h"]
    if reject["tag"] == "A" and accept["tag"] != "A" and not reject["filled"] and not reject["bordered"]:
        return False, "Reject is a plain text link; Accept is a button"
    if a_area and r_area / a_area < MIN_AREA_SHARE:
        return False, f"Reject is {round(100 * r_area / a_area)}% of Accept's size"
    if accept["font"] and reject["font"] / accept["font"] < MIN_FONT_SHARE:
        return False, f"Reject's text is {round(100 * reject['font'] / accept['font'])}% of Accept's size"
    if accept["filled"] and not reject["filled"] and not reject["bordered"]:
        return False, "Accept is a filled button; Reject has no fill or outline"
    return True, "Reject has comparable size and style"


async def measure_buttons(page: Page) -> dict | None:
    """The Accept and Reject controls as the page shows them, or None when either was not found on the first layer."""
    try:
        got = await page.evaluate(_MEASURE_JS, [list(ACCEPT_WORDS), list(REJECT_WORDS)])
    except PlaywrightError:
        return None
    if not got or not got.get("accept") or not got.get("reject"):
        return None
    comparable, why = judge(got["accept"], got["reject"])
    return {"comparable": comparable, "why": why, "accept": {k: got["accept"][k] for k in ("tag", "w", "h", "font", "filled")}, "reject": {k: got["reject"][k] for k in ("tag", "w", "h", "font", "filled")}}


async def run_button_comparison(make_context: Callable[[], Awaitable], root_url: str, *, goto: Callable[[Page, str], Awaitable[bool]], settle_ms: int = 2500) -> dict | None:
    """One fresh session: load the home page and measure the first-layer Accept and Reject controls. None when the banner or either control was not found (nothing is invented)."""
    context = await make_context()
    try:
        page = await context.new_page()
        if not await goto(page, root_url):
            return None
        try:
            await page.wait_for_load_state("load", timeout=15_000)
        except PlaywrightError:
            pass
        await page.wait_for_timeout(settle_ms)
        await pass_consent_gate(page)
        return await measure_buttons(page)
    except Exception as exc:  # noqa: BLE001 — an observation that could not be made is simply absent
        logger.warning("The Accept and Reject comparison for %s could not be made: %s", root_url, exc)
        return None
    finally:
        try:
            await context.close()
        except PlaywrightError:
            pass


REJECT_REASONS = {
    "homepage_not_loaded": "The home page did not load, so Reject on further pages was not tested.",
    "reject_not_clicked": "Reject could not be clicked and confirmed, so Reject on further pages was not tested.",
    "error": "The test of Reject on further pages could not be completed.",
}


async def run_reject_persistence(make_context: Callable[[], Awaitable], root_url: str, further_urls: list[str], *, goto: Callable[[Page, str], Awaitable[bool]], settle_ms: int = 2500) -> dict:
    """Reject in one fresh session, then up to two further pages in the same session. {"tested", "reason", "non_essential_after_reject": [...], "further_pages": [{url, loaded, non_essential}], "requests_only": True}."""
    site = urlparse(root_url).netloc
    result: dict = {"tested": False, "reason": None, "non_essential_after_reject": [], "further_pages": [], "requests_only": True}
    context = await make_context()
    try:
        page = await context.new_page()
        records: list[tuple[str, str]] = []
        await _watch(page, records)
        if not await goto(page, root_url):
            return {**result, "reason": "homepage_not_loaded"}
        try:
            await page.wait_for_load_state("load", timeout=15_000)
        except PlaywrightError:
            pass
        await page.wait_for_timeout(settle_ms)
        await pass_consent_gate(page)
        if await click_reject(page) != "clicked" or not await confirm_interaction_completed(page, accept=False):
            return {**result, "reason": "reject_not_clicked"}
        await page.wait_for_timeout(500)
        mark = len(records)
        await page.wait_for_timeout(settle_ms)
        result["non_essential_after_reject"] = non_essential_requests(records[mark:], site)
        for url in further_urls[:2]:
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
        result["tested"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("The test of Reject on further pages for %s did not complete: %s", root_url, exc)
        return {**result, "tested": False, "reason": "error"}
    finally:
        try:
            await context.close()
        except PlaywrightError:
            pass
