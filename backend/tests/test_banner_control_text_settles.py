"""The home page's control text is read again until it stops growing (scanner/crawler.py _settled_control_text), in a real headless browser on an offline page.

17 Nov 2026: the same jambulogix.com banner was read with "Reject all" and "Manage preferences" in it once (per-purpose choices found, score 86) and twice without them (82): the buttons arrive after the first line of text.
"""

import pytest

from app.scanner import crawler
from tests._browser import run_with_browser

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

LATE = """<html><body><div id="b"><p>We use cookies.</p><button>Accept all</button></div>
<script>setTimeout(() => {{ const d = document.getElementById('b');
  for (const t of ['Reject all', 'Manage preferences']) {{ const x = document.createElement('button'); x.textContent = t; d.appendChild(x); }} }}, {delay});</script></body></html>"""


async def _read(html: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html)
            return await crawler._settled_control_text(page)
        finally:
            await browser.close()


def test_buttons_that_arrive_after_the_first_reading_are_in_the_text():
    text = run_with_browser(lambda: _read(LATE.format(delay=900)))
    assert "Accept all" in text and "Reject all" in text and "Manage preferences" in text, text


REJECT_LATE = """<html><body><div id="b"><p>We use cookies.</p><button>Accept all</button><button>Manage preferences</button></div>
<script>setTimeout(() => {{ const x = document.createElement('button'); x.textContent = 'Reject all'; document.getElementById('b').appendChild(x); }}, {delay});</script></body></html>"""


def test_a_reject_button_drawn_after_accept_and_preferences_is_still_waited_for():
    """18 Nov 2026: the live banner was read with Accept and Manage preferences but not yet Reject, and recorded as having no reject-all option (a false Fail)."""
    text = run_with_browser(lambda: _read(REJECT_LATE.format(delay=3200)))       # later than the first readings, inside the extra window
    assert "Accept all" in text and "Manage preferences" in text and "Reject all" in text, text


def test_a_banner_that_really_has_no_reject_is_read_after_a_bounded_wait():
    import time

    started = time.monotonic()
    text = run_with_browser(lambda: _read("<html><body><button>Accept all</button><button>Manage preferences</button></body></html>"))
    assert "Accept all" in text and "Reject" not in text
    assert time.monotonic() - started < 15          # about 1.4 s of rereads plus at most about 4.2 s extra, plus the browser start


def test_a_banner_that_is_complete_at_once_is_read_unchanged():
    text = run_with_browser(lambda: _read("<html><body><button>Accept all</button><button>Reject all</button></body></html>"))
    assert "Accept all" in text and "Reject all" in text


def test_a_page_with_no_controls_reads_empty_not_an_error():
    assert not (run_with_browser(lambda: _read("<html><body><p>Nothing to click</p></body></html>")) or "").strip()
