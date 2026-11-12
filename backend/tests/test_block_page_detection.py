"""A site's bot-protection page can answer HTTP 200 with an ordinary title (consiva.ai: "Automated access is not permitted", reference: webdriver). It must be treated like a
403: no score or findings about the block screen, and no screenshot of it."""
import asyncio

from app.scanner import crawler

BLOCK = "Automated access is not permitted\nThis page can only be used from a regular browser session. If you believe this is a mistake, contact support@consiva.ai.\nReference: webdriver"


class _Page:
    def __init__(self, text, status=200):
        self._text, self._status = text, status

    async def evaluate(self, _script):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text

    async def goto(self, url, **_kw):
        return type("R", (), {"status": self._status, "headers": {}})()


def _run(coro):
    return asyncio.run(coro)


def test_the_consiva_block_page_is_recognised():
    assert _run(crawler._page_is_a_block_page(_Page(BLOCK))) is True


def test_a_real_page_is_not_a_block_page():
    assert _run(crawler._page_is_a_block_page(_Page("Welcome to our shop. " * 100))) is False
    # a long privacy notice that mentions access being denied in passing is not a block either
    assert _run(crawler._page_is_a_block_page(_Page("We may deny access to the service. Your access has been denied if you breach the terms. " + "x " * 800))) is False
    assert _run(crawler._page_is_a_block_page(_Page(""))) is False


def test_a_page_that_cannot_be_read_is_not_called_blocked():
    assert _run(crawler._page_is_a_block_page(_Page(RuntimeError("closed")))) is False


def test_navigation_to_a_200_block_page_fails_like_a_403():
    _resp, status, exc, _attempts = _run(crawler._navigate_with_retry(_Page(BLOCK), "https://x.test/", 1000, window_s=0))
    assert status == "failed" and getattr(exc, "status", None) == 403 or "403" in str(exc)


def test_navigation_to_a_normal_page_still_succeeds():
    _resp, status, exc, _attempts = _run(crawler._navigate_with_retry(_Page("Welcome " * 50), "https://x.test/", 1000, window_s=0))
    assert status == "success" and exc is None
