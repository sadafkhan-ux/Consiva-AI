"""A consent widget's stylesheet inside its shadow root is not page text (scanner/crawler._SHADOW_DOM_TEXT_JS).

Runs a real headless browser on an offline page: no network, no site. The policy text of a page was once 60,000 characters, almost all of it CSS from such a widget, and was cut
before the sharing and retention sections (digitalanumati.com).
"""

import asyncio
import sys

import pytest

from app.scanner.crawler import _SHADOW_DOM_TEXT_JS

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

CSS = ".da-user-consent-root .opacity-75{opacity:.75!important}" * 400
HTML = f"""<html><body><h1>Privacy Policy</h1><p>We share data with analytics providers.</p><div id="host"></div>
<script>const r = document.getElementById('host').attachShadow({{mode: 'open'}});
r.innerHTML = '<style>{CSS}</style><script>var x = 1;<\\/script><div>Manage cookie preferences</div><template><p>hidden template</p></template>';</script></body></html>"""


def _run(coro):
    """Its own event loop. Other tests leave Windows on the selector loop, which cannot start the browser's subprocess; the proactor loop can."""
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _text() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(HTML)
        out = await page.evaluate(_SHADOW_DOM_TEXT_JS)
        await browser.close()
        return out


def test_the_widgets_stylesheet_and_scripts_are_not_page_text():
    text = _run(_text())
    assert "Privacy Policy" in text and "We share data with analytics providers" in text     # the page itself
    assert "Manage cookie preferences" in text                                                # a visible shadow-root label is still read
    assert "opacity" not in text and "da-user-consent-root" not in text and "var x" not in text and "hidden template" not in text
    assert len(text) < 500


# ── a policy page that is still adding text when it is first read ──
LATE_HTML = """<html><body><h1>Privacy Policy</h1><p>We collect your name.</p><div id="more"></div>
<script>setTimeout(() => { document.getElementById('more').innerHTML = '<p>' + 'You may withdraw consent at any time. '.repeat(20) + '</p>'; }, 1300);</script></body></html>"""


async def _late_text(url: str) -> str:
    from app.scanner.crawler import _settled_text

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(LATE_HTML)
        out = await _settled_text(page, url)
        await browser.close()
        return out


def test_a_policy_page_is_read_again_until_its_text_stops_growing():
    assert "withdraw consent" in _run(_late_text("https://x.test/privacy"))


def test_an_ordinary_page_is_read_once():
    assert "withdraw consent" not in _run(_late_text("https://x.test/products"))
