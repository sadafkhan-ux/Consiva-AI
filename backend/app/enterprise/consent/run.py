"""The consent tests of an Enterprise scan: what the site does after Accept, after Reject, when a choice is withdrawn, and when only one purpose is switched on.

Every test runs in its own fresh browser session, so no state (cookies, storage, a recorded choice) leaks from one into another, and every session has the crawl's address guard on.
What the site does BEFORE any choice is recorded for every crawled page by the crawl itself.

The mechanics of finding and clicking a consent banner's controls, of reopening the choice and withdrawing it, and of switching purposes one at a time already exist in the 5-page
scanner and work on real banners (OneTrust, Cookiebot, Consiva's own and others). They are called here as they are, unchanged, with this crawl's session factory and navigation: that is
reuse of working code, not a dependency on the 5-page agent's scoring. What each test OBSERVED is judged by this product's own rules (app/enterprise/assess/consent.py).

A test that could not be carried out says why (no banner, a control not found, the page did not load) and is recorded as not tested. It is never recorded as a failure of the site.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Page

from app.enterprise.crawl.page_inspect import normalize_cookies, summarise_requests
from app.scanner.consent_interactor import click_accept, click_reject, confirm_interaction_completed, pass_consent_gate
from app.scanner.purposes import run_purpose_test
from app.scanner.reject_and_compare import run_button_comparison, run_reject_persistence
from app.scanner.withdrawal import non_essential_requests, run_withdrawal_test

logger = logging.getLogger(__name__)

SETTLE_MS = 3000
NAV_TIMEOUT_MS = 30_000
MAX_PARALLEL = 3
BANNER_TEXT_MAX = 6000

#: The text of the consent banner as the test sees it, read just before the click: dialogs and elements whose id or class names a cookie, consent, CMP or banner layer, and every
#: shadow root (consent tools draw there). The homepage's own capture can miss a banner that draws late; the banner the test clicked is the one the method 3 banner checks
#: (C07-C10, C15) must read. Text only: no form value, no cookie.
_BANNER_TEXT_JS = r"""() => {
  const sel = '[role=dialog],[aria-modal=true],[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=cmp i],[class*=cmp i],[id*=banner i],[class*=banner i]';
  const out = []; const seen = new Set();
  const take = (t) => { t = (t || '').trim(); if (t.length >= 20 && !seen.has(t)) { seen.add(t); out.push(t); } };
  const walk = (root, depth) => {
    if (depth > 6) return;
    root.querySelectorAll(sel).forEach((el) => take(el.innerText || el.textContent));
    root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) { take(el.shadowRoot.textContent); walk(el.shadowRoot, depth + 1); } });
  };
  walk(document, 0);
  return out.join('\n').slice(0, %d);
}""" % BANNER_TEXT_MAX


#: The wording of a gate a consent tool shows BEFORE its choice (an age or parent/guardian question), for K01-K05. Only what a visitor sees: the visible text of the shadow roots
#: and consent layers, with style sheets and scripts left out (a consent tool's style sheet alone can fill the banner capture above) and short button labels kept
#: ("Yes, I am 18+"). Read only when a gate was found; the banner capture above is unchanged.
_GATE_TEXT_JS = r"""() => {
  const out = []; const seen = new Set();
  const take = (t) => { (t || '').split('\n').forEach((x) => { x = x.trim(); if (x.length >= 2 && !seen.has(x)) { seen.add(x); out.push(x); } }); };
  const visible = (root) => { Array.from(root.children).forEach((el) => { if (!['STYLE', 'SCRIPT', 'TEMPLATE', 'LINK'].includes(el.tagName)) take(el.innerText); }); };
  const walk = (root, depth) => {
    if (depth > 6) return;
    root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) { visible(el.shadowRoot); walk(el.shadowRoot, depth + 1); } });
  };
  walk(document, 0);
  document.querySelectorAll('[aria-modal=true],[id*=cmp i],[class*=cmp i],[id*=age-gate i],[class*=age-gate i],[id*=agegate i],[class*=agegate i]').forEach((el) => take(el.innerText));
  return out.join('\n').slice(0, %d);
}""" % BANNER_TEXT_MAX


async def _gate_text(page: Page) -> str:
    try:
        return str(await page.evaluate(_GATE_TEXT_JS) or "")
    except Exception:                                                  # noqa: BLE001
        return ""


async def _banner_text(page: Page) -> str:
    try:
        return str(await page.evaluate(_BANNER_TEXT_JS) or "")
    except Exception:                                                  # noqa: BLE001
        return ""


def make_session_tools(browser, settings, guard, new_context_kwargs) -> tuple[Callable[[], Awaitable], Callable[[Page, str], Awaitable[bool]]]:
    """(make_context, goto): a fresh guarded browser session, and a navigation that says whether the page loaded."""

    async def make_context():
        context = await browser.new_context(**new_context_kwargs(settings))
        await context.route("**/*", guard)
        return context

    async def goto(page: Page, url: str) -> bool:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return bool(response and response.status < 400)
        except Exception:
            return False

    return make_context, goto


async def observe_state(make_context, goto, root_url: str, *, accept: bool) -> dict:
    """One session: load the entry page, give the choice (Accept or Reject), and record what is then in the browser: the cookies, and the requests made after the choice."""
    state = "post_accept" if accept else "post_reject"
    base: dict = {"state": state, "tested": False, "reason": None}
    context = await make_context()
    try:
        page = await context.new_page()
        records: list[tuple[str, str]] = []
        page.on("request", lambda r: records.append((r.url, r.resource_type)))
        if not await goto(page, root_url):
            return {**base, "reason": "page_not_loaded"}
        try:
            await page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(SETTLE_MS)
        # A gate the consent tool shows first (an age or parent/guardian question) is evidence for K01-K05: its wording is read BEFORE the scanner answers it, and what was
        # answered is kept beside the result (additive keys; older scans simply do not have them).
        before_gate = await _gate_text(page)
        gate = await pass_consent_gate(page)
        if gate:
            base = {**base, "consent_gate": gate, "gate_text": before_gate}
        banner_text = await _banner_text(page)                       # the banner about to be clicked, before the click closes it
        # What counts is everything requested from the moment of the click: a tag released by Accept fires at once, not after a pause.
        mark = len(records)
        clicked = await (click_accept if accept else click_reject)(page)
        if clicked != "clicked":
            return {**base, "reason": "control_not_found" if clicked == "not_found" else "click_failed", "banner_text": banner_text}
        confirmed = await confirm_interaction_completed(page, accept=accept)
        await page.wait_for_timeout(SETTLE_MS)
        after = records[mark:]
        site = urlparse(root_url).netloc
        return {
            **base, "tested": True, "confirmed": bool(confirmed), "page_url": root_url, "banner_text": banner_text,
            "cookies": normalize_cookies(await context.cookies(), root_url, state),
            "non_essential": non_essential_requests(after, site), "requests": summarise_requests([{"url": u, "type": t} for u, t in after], root_url, state),
        }
    except Exception as exc:
        logger.warning("consent test %s of %s did not complete: %s", state, root_url, exc)
        return {**base, "reason": "error"}
    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _withdrawal(make_context, goto, root_url: str, further_urls: list[str]) -> dict:
    context = await make_context()
    try:
        return await run_withdrawal_test(context, root_url, further_urls, goto=goto)
    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _comparison(make_context, goto, root_url: str) -> dict:
    result = await run_button_comparison(make_context, root_url, goto=goto)
    return result if result is not None else {"tested": False, "reason": "banner_or_controls_not_found"}


async def run_consent_suite(make_context, goto, root_url: str, further_urls: list[str], *, max_parallel: int = MAX_PARALLEL) -> dict[str, dict]:
    """All the consent tests of one site, at most `max_parallel` sessions at a time (the crawl runner passes 1: one browser session at a time). Returns {test name: its result}. Never raises."""
    gate = asyncio.Semaphore(max(1, max_parallel))

    async def guarded(name: str, coro_fn) -> tuple[str, dict]:
        async with gate:
            try:
                result = await coro_fn()
            except Exception as exc:
                logger.warning("consent test %s of %s failed to start: %s", name, root_url, exc)
                result = {"tested": False, "reason": "error"}
        return name, (result if isinstance(result, dict) else {"tested": False, "reason": "error"})

    jobs = [
        ("accept", lambda: observe_state(make_context, goto, root_url, accept=True)),
        ("reject", lambda: observe_state(make_context, goto, root_url, accept=False)),
        ("reject_persistence", lambda: run_reject_persistence(make_context, root_url, further_urls, goto=goto)),
        ("withdrawal", lambda: _withdrawal(make_context, goto, root_url, further_urls)),
        ("purposes", lambda: run_purpose_test(make_context, root_url, goto=goto)),
        ("button_comparison", lambda: _comparison(make_context, goto, root_url)),
    ]
    return dict(await asyncio.gather(*(guarded(n, f) for n, f in jobs)))
