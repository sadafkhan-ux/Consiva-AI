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
        await pass_consent_gate(page)
        # What counts is everything requested from the moment of the click: a tag released by Accept fires at once, not after a pause.
        mark = len(records)
        clicked = await (click_accept if accept else click_reject)(page)
        if clicked != "clicked":
            return {**base, "reason": "control_not_found" if clicked == "not_found" else "click_failed"}
        confirmed = await confirm_interaction_completed(page, accept=accept)
        await page.wait_for_timeout(SETTLE_MS)
        after = records[mark:]
        site = urlparse(root_url).netloc
        return {
            **base, "tested": True, "confirmed": bool(confirmed), "page_url": root_url, "cookies": normalize_cookies(await context.cookies(), root_url, state),
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
