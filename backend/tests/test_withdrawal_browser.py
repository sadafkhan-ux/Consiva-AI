"""The withdrawal test (scanner/withdrawal.py) in a real headless browser, on an offline site: no network, no real website, no consent vendor.

The fake site has a consent banner (Accept all, Reject all), a footer control that reopens the choice, and an analytics beacon (a request to a Google Analytics address, answered here).
Three versions of it: one that honours a withdrawal, one that ignores it, and one with no way to reopen the choice. The test must tell them apart by what it observes.
"""

import pytest

from app.scanner.withdrawal import non_essential_requests, run_withdrawal_test
from tests._browser import run_with_browser

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

BEACON = "https://www.google-analytics.com/g/collect"

PAGE = """<html><head><title>{title}</title></head><body><h1>{title}</h1>
<div id="banner" {banner_hidden}><p>We use cookies.</p><button id="a" onclick="choose('accepted')">Accept all</button><button id="r" onclick="choose('rejected')">Reject all</button></div>
{footer}
<div id="dlg" style="display:none"><p>Cookie preferences</p><button id="w" onclick="withdraw()">Reject all</button></div>
<script>
const MODE = "{mode}";
function state() {{ return localStorage.getItem('consent'); }}
function beacon() {{ fetch('{beacon}?v=2&n=' + Date.now(), {{mode: 'no-cors'}}).catch(() => {{}}); }}
function choose(v) {{ localStorage.setItem('consent', v); document.getElementById('banner').style.display = 'none'; if (v === 'accepted') beacon(); }}
function reopen() {{ document.getElementById('dlg').style.display = 'block'; }}
function withdraw() {{ if (MODE === 'honours') localStorage.setItem('consent', 'withdrawn'); document.getElementById('dlg').style.display = 'none'; }}
// the site's own tag: fires while it believes consent is given
setInterval(() => {{ if (state() === 'accepted') beacon(); }}, 300);
if (state() === 'accepted') beacon();
</script></body></html>"""

FOOTER = '<footer><button id="s" onclick="reopen()">Cookie settings</button></footer>'


def _page(mode: str, title: str, reopen: bool = True) -> str:
    return PAGE.format(title=title, mode=mode, beacon=BEACON, footer=FOOTER if reopen else "<footer><a href='/privacy'>Privacy</a></footer>", banner_hidden="")


async def _run(mode: str, reopen: bool = True) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()

        async def handle(route):
            url = route.request.url
            if url.startswith(BEACON):
                return await route.fulfill(status=204, body="")
            if url.startswith("https://site.test/"):
                path = url.removeprefix("https://site.test")
                title = {"/": "Home", "/a": "Page A", "/b": "Page B"}.get(path.split("?")[0], "Other")
                return await route.fulfill(status=200, content_type="text/html", body=_page(mode, title, reopen))
            return await route.abort()

        await context.route("**/*", handle)

        async def goto(page, url):
            await page.goto(url)
            return True

        try:
            return await run_withdrawal_test(context, "https://site.test/", ["https://site.test/a", "https://site.test/b", "https://site.test/c"], goto=goto, settle_ms=900)
        finally:
            await browser.close()


def test_a_site_that_honours_withdrawal_is_seen_to_stop_on_the_page_and_on_two_further_pages():
    r = run_with_browser(lambda: _run("honours"))
    assert r["tested"] is True and r["reason"] is None, r
    assert r["reopen_control"].lower().startswith("cookie settings") and "reject all" in r["withdraw_control"].lower()
    assert r["accepted_non_essential"], "Accept started analytics, so there was activity to stop"
    assert r["after_withdrawal"] == []
    assert [p["url"] for p in r["further_pages"]] == ["https://site.test/a", "https://site.test/b"]       # two further pages, no more
    assert all(p["loaded"] and p["non_essential"] == [] for p in r["further_pages"])
    assert r["requests_only"] is True


def test_a_site_that_ignores_withdrawal_is_seen_to_keep_contacting_analytics():
    r = run_with_browser(lambda: _run("ignores"))
    assert r["tested"] is True, r
    assert r["after_withdrawal"] and r["after_withdrawal"][0]["vendor"] == "Google Analytics" and r["after_withdrawal"][0]["category"] == "analytics"
    assert all(p["non_essential"] for p in r["further_pages"])


def test_a_site_with_no_way_to_reopen_the_choice_is_not_tested_and_says_why():
    r = run_with_browser(lambda: _run("honours", reopen=False))
    assert r["tested"] is False and r["reason"] == "no_reopen_control" and r["reopen_control"] is None
    assert r["accepted_non_essential"], "what Accept started is still recorded"


def test_the_tag_loader_is_not_counted_as_tracking_and_a_beacon_is():
    reqs = [("https://www.googletagmanager.com/gtm.js?id=GTM-X", "script"), (BEACON + "?v=2", "fetch"), ("https://fonts.gstatic.com/x.woff2", "font")]
    got = non_essential_requests(reqs, "site.test")
    assert [g["vendor"] for g in got] == ["Google Analytics"]


def test_the_browser_reports_a_box_a_script_ticked_which_the_markup_does_not_show():
    from app.scanner.crawler import _FORM_CHECKED_JS
    from app.scanner.page_parser import parse_page

    html = '<form id="f"><input type="email" name="e"><input type="hidden" name="h"><input type="checkbox" name="c"><input type="radio" name="r"></form><script>document.querySelector("[name=c]").checked = true;</script>'

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html)
            states = await page.evaluate(_FORM_CHECKED_JS)
            markup = await page.content()
            await browser.close()
            return states, markup

    states, markup = run_with_browser(go)
    assert states == [[None, True, False]]                       # the email field, the box the script ticked, the radio button; the hidden input is not listed
    assert "checked" not in markup.split("<script>")[0]            # the markup alone would have said it was not ticked
    box = parse_page(markup, "https://x.test/", states).forms[0].fields
    assert [f.checked for f in box] == [None, True, False]
