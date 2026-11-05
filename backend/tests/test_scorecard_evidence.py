"""The evidence the scorecard needs beyond the 24 detailed checks: the banner on each page, browser storage names, insecure content on form pages, Accept and Reject side by side, and Reject on further pages.

The browser tests run a real headless browser on an offline fake site (no network, no real website, no consent vendor).
"""

import pytest

from app.config import get_settings
from app.scanner.crawler import _SHADOW_DOM_CONTROL_TEXT_JS, _crawl_full_site
from app.scanner.page_signals import (
    banner_on_page,
    merge_mixed_content,
    merge_storage_keys,
    tracking_storage_keys,
)
from app.scanner.reject_and_compare import (
    judge,
    measure_buttons,
    run_button_comparison,
    run_reject_persistence,
)
from app.scanner.scan_profiles import WEB, resolve_profile
from tests._browser import run_with_browser
from tests.test_scanner_coverage import ROOT, _FakeContext, _FakePage

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

GA = "https://www.google-analytics.com/g/collect"


# ── page signals (pure) ──
def test_storage_key_names_are_kept_and_values_never_are():
    keys = ["_ga", "ajs_anonymous_id", "theme", "cart", "hjSessionUser_123", "li_gc"]
    assert tracking_storage_keys(keys) == ["_ga", "ajs_anonymous_id", "hjSessionUser_123", "li_gc"]
    acc = {}
    merge_storage_keys(acc, {"local": ["_ga", "theme"], "session": ["ajs_user_id"]})
    merge_storage_keys(acc, {"local": ["_ga"], "session": []})
    merge_storage_keys(acc, None)                                        # a page whose storage could not be read is not counted as clean
    assert acc == {"pages_checked": 2, "keys": ["_ga", "theme", "ajs_user_id"], "pre_consent_tracking_keys": ["_ga", "ajs_user_id"]}


def test_insecure_content_is_recorded_only_for_pages_that_collect_details():
    acc = {}
    merge_mixed_content(acc, page_url="https://x.test/contact", has_form=True, insecure=["http://cdn.test/a.js"])
    merge_mixed_content(acc, page_url="https://x.test/blog", has_form=False, insecure=["http://cdn.test/b.js"])
    merge_mixed_content(acc, page_url="https://x.test/signup", has_form=True, insecure=[])
    assert acc == {"pages_checked": 3, "pages_with_insecure_content": ["https://x.test/contact"]}


def test_a_page_with_a_consent_banner_is_told_from_one_without():
    with_banner = banner_on_page(scripts=[], visible_text="", global_vars=[], control_text="we use cookies accept all reject all")
    without = banner_on_page(scripts=[], visible_text="Welcome to our shop", global_vars=[], control_text="add to cart")
    assert with_banner is True and without is False


# ── the crawl records them ──
class _BannerPage(_FakePage):
    """A page whose rendered controls are what the markup shows (the stock fake reports none)."""

    async def evaluate(self, script, arg=None):
        if script == _SHADOW_DOM_CONTROL_TEXT_JS:
            return "accept all reject all we use cookies" if "cookie-banner" in self._pages[self._url] else ""
        return await super().evaluate(script, arg)


class _BannerContext(_FakeContext):
    async def new_page(self):
        return _BannerPage(self.pages, self.failures, self.navigations)


async def test_the_crawl_records_the_banner_on_each_page_the_storage_names_and_insecure_content():
    banner_html = '<html><body><div class="cookie-banner"><button>Accept all</button><button>Reject all</button></div><a href="/a">a</a><form><input type="email" name="e"></form></body></html>'
    pages = {f"{ROOT}/": banner_html, f"{ROOT}/a": "<html><body>No banner here</body></html>"}
    settings = get_settings()
    _r, forms, *_rest, diagnostics = await _crawl_full_site(_BannerContext(pages), ROOT, settings, disallowed_prefixes=set(), profile=resolve_profile(WEB, settings), deadline_monotonic=None)
    by_page = {x["url"].removeprefix(ROOT) or "/": x["banner"] for x in diagnostics["banner_by_page"]}
    assert by_page.get("/") is True and by_page.get("/a") is False                            # the banner is on the home page and not on the inner one
    assert diagnostics["storage_signals"].get("pages_checked", 0) == 0                        # the stock fake cannot report storage: nothing is claimed
    assert diagnostics["mixed_content"] == {"pages_checked": 2, "pages_with_insecure_content": []}
    assert len(forms) == 1


# ── Accept and Reject side by side ──
def test_reject_is_less_prominent_by_stated_rules_only():
    button = {"tag": "BUTTON", "w": 140, "h": 40, "font": 16, "filled": True, "bordered": False}
    assert judge(button, {**button})[0] is True
    assert judge(button, {**button, "tag": "A", "filled": False, "bordered": False}) == (False, "Reject is a plain text link; Accept is a button")
    assert judge(button, {**button, "w": 60, "h": 20})[0] is False and "size" in judge(button, {**button, "w": 60, "h": 20})[1]
    assert judge(button, {**button, "font": 11})[0] is False
    assert judge(button, {**button, "filled": False, "bordered": False})[0] is False
    assert judge(button, {**button, "filled": False, "bordered": True})[0] is True               # an outlined button is a button


BANNER = """<html><body><div id="b" style="position:fixed;bottom:0">{accept}{reject}</div></body></html>"""
EQUAL = '<button style="background:#06c;color:#fff;font-size:16px;padding:10px 24px">Accept all</button><button style="background:#eee;color:#000;font-size:16px;padding:10px 24px">Reject all</button>'
LINK = '<button style="background:#06c;color:#fff;font-size:16px;padding:10px 24px">Accept all</button><a href="#" style="font-size:11px;background:none">Reject all</a>'


async def _measure(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html)
        out = await measure_buttons(page)
        await browser.close()
        return out


def test_equal_buttons_are_comparable_and_a_text_link_is_not():
    eq = run_with_browser(lambda: _measure(BANNER.format(accept="", reject="") .replace("</div>", EQUAL + "</div>")))
    assert eq and eq["comparable"] is True
    lk = run_with_browser(lambda: _measure(BANNER.format(accept="", reject="").replace("</div>", LINK + "</div>")))
    assert lk and lk["comparable"] is False and "plain text link" in lk["why"]
    assert lk["accept"]["tag"] == "BUTTON" and lk["reject"]["tag"] == "A"                         # the numbers behind the verdict are kept
    none = run_with_browser(lambda: _measure("<html><body><p>No banner</p></body></html>"))
    assert none is None


# ── Reject on further pages ──
REJECT_PAGE = """<html><body><h1>{title}</h1><div id="b"><button onclick="r()">Accept all</button><button onclick="r()">Reject all</button></div>
<script>
const LEAKY = {leaky};
function fire() {{ fetch('{ga}?n=' + Date.now(), {{mode: 'no-cors'}}).catch(() => {{}}); }}
function r() {{ localStorage.setItem('consent', 'rejected'); document.getElementById('b').style.display = 'none'; }}
if (LEAKY && location.pathname !== '/') fire();
</script></body></html>"""


async def _reject(leaky: bool) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def make():
            context = await browser.new_context()

            async def handle(route):
                url = route.request.url
                if url.startswith(GA):
                    return await route.fulfill(status=204, body="")
                if url.startswith("https://site.test/"):
                    path = url.removeprefix("https://site.test").split("?")[0] or "/"
                    return await route.fulfill(status=200, content_type="text/html", body=REJECT_PAGE.format(title=path, leaky="true" if leaky else "false", ga=GA))
                return await route.abort()

            await context.route("**/*", handle)
            return context

        async def goto(page, url):
            await page.goto(url)
            return True

        try:
            return await run_reject_persistence(make, "https://site.test/", ["https://site.test/a", "https://site.test/b", "https://site.test/c"], goto=goto, settle_ms=700)
        finally:
            await browser.close()


def test_reject_that_holds_on_two_further_pages_is_seen_to_hold():
    r = run_with_browser(lambda: _reject(False))
    assert r["tested"] is True and r["non_essential_after_reject"] == []
    assert [p["url"] for p in r["further_pages"]] == ["https://site.test/a", "https://site.test/b"] and all(p["loaded"] and p["non_essential"] == [] for p in r["further_pages"])


def test_reject_that_does_not_hold_on_a_further_page_is_seen_to_leak():
    r = run_with_browser(lambda: _reject(True))
    assert r["tested"] is True and all(p["non_essential"] and p["non_essential"][0]["category"] == "analytics" for p in r["further_pages"])


def test_a_site_with_no_banner_is_not_tested_and_says_why():
    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            async def make():
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body>No banner</body></html>"))
                return context

            async def goto(page, url):
                await page.goto(url)
                return True

            try:
                return await run_reject_persistence(make, "https://site.test/", [], goto=goto, settle_ms=300), await run_button_comparison(make, "https://site.test/", goto=goto, settle_ms=300)
            finally:
                await browser.close()

    rp, cmp_ = run_with_browser(go)
    assert rp["tested"] is False and rp["reason"] == "reject_not_clicked" and cmp_ is None
