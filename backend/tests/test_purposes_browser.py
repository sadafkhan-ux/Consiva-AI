"""Choices by purpose (scanner/purposes.py) in a real headless browser on an offline site: no network, no real website, no consent vendor.

The fake consent tool has a banner (Accept all, Reject all, Customize) and a preferences dialog with three switches (Strictly necessary, locked; Analytics; Marketing) and a save button. Three versions of
the site: one where each switch does only what it says, one where switching on Analytics also lets advertising through, and one with no way to open the preferences.
"""

import pytest

from app.scanner.purposes import kind_of, run_purpose_test, verdict
from tests._browser import run_with_browser

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

GA = "https://www.google-analytics.com/g/collect"
PIXEL = "https://connect.facebook.net/en_US/fbevents.js"

PAGE = """<html><body><h1>Home</h1>
<div id="banner"><p>We use cookies.</p><button onclick="document.getElementById('banner').style.display='none'">Accept all</button>
<button onclick="document.getElementById('banner').style.display='none'">Reject all</button>{customize}</div>
<div id="prefs" style="display:none"><h2>Cookie preferences</h2>
<label><input type="checkbox" id="c0" checked disabled> Strictly necessary (always active)</label>
<label><input type="checkbox" id="c1"> Analytics cookies</label>
<label><input type="checkbox" id="c2"> Marketing cookies</label>
<button onclick="save()">Save my choices</button></div>
<script>
const LEAKY = {leaky};
function fire(u) {{ fetch(u + '?n=' + Date.now(), {{mode: 'no-cors'}}).catch(() => {{}}); }}
function save() {{
  const analytics = document.getElementById('c1').checked, marketing = document.getElementById('c2').checked;
  document.getElementById('prefs').style.display = 'none';
  if (analytics) {{ fire('{ga}'); if (LEAKY) fire('{pixel}'); }}
  if (marketing) fire('{pixel}');
}}
</script></body></html>"""

CUSTOMIZE = "<button onclick=\"document.getElementById('prefs').style.display='block'\">Customize</button>"


async def _run(leaky: bool = False, customize: bool = True) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def make():
            context = await browser.new_context()

            async def handle(route):
                url = route.request.url
                if url.startswith((GA, PIXEL)):
                    return await route.fulfill(status=204, body="")
                if url.startswith("https://site.test/"):
                    body = PAGE.format(customize=CUSTOMIZE if customize else "", leaky="true" if leaky else "false", ga=GA, pixel=PIXEL)
                    return await route.fulfill(status=200, content_type="text/html", body=body)
                return await route.abort()

            await context.route("**/*", handle)
            return context

        async def goto(page, url):
            await page.goto(url)
            return True

        try:
            return await run_purpose_test(make, "https://site.test/", goto=goto, settle_ms=800)
        finally:
            await browser.close()


def test_each_purpose_is_switched_on_alone_and_watched_independently():
    r = run_with_browser(lambda: _run())
    assert r["tested"] is True and r["reason"] is None, r
    by = {p["kind"]: p for p in r["purposes"]}
    assert set(by) == {"analytics", "marketing"}                                    # the locked, strictly necessary switch is not a choice
    assert [o["category"] for o in by["analytics"]["observed"]] == ["analytics"] and by["analytics"]["verdict"] == "as_expected"
    assert [o["category"] for o in by["marketing"]["observed"]] == ["marketing"] and by["marketing"]["verdict"] == "as_expected"
    assert r["requests_only"] is True


def test_a_purpose_that_lets_another_purposes_tracking_through_is_unexpected():
    r = run_with_browser(lambda: _run(leaky=True))
    by = {p["kind"]: p for p in r["purposes"]}
    assert by["analytics"]["verdict"] == "unexpected"
    assert {o["category"] for o in by["analytics"]["observed"]} == {"analytics", "marketing"}      # advertising fired with only Analytics on
    assert by["marketing"]["verdict"] == "as_expected"


def test_a_tool_with_no_way_to_open_the_preferences_is_not_tested_and_says_why():
    r = run_with_browser(lambda: _run(customize=False))
    assert r["tested"] is False and r["reason"] == "no_preferences_control" and r["purposes"] == []


def test_purposes_are_named_from_the_words_on_the_switch_and_judged_only_on_what_was_contacted():
    assert kind_of("Analytics cookies") == "analytics" and kind_of("Targeting and advertising") == "marketing" and kind_of("Remember my preferences") == "functional" and kind_of("Social media") == "other"
    ga = {"vendor": "Google Analytics", "category": "analytics", "host": "g.test"}
    px = {"vendor": "Meta Pixel", "category": "marketing", "host": "p.test"}
    assert verdict("analytics", [ga]) == "as_expected" and verdict("analytics", [ga, px]) == "unexpected"
    assert verdict("functional", []) == "as_expected" and verdict("functional", [ga]) == "unexpected" and verdict("marketing", [px]) == "as_expected"
