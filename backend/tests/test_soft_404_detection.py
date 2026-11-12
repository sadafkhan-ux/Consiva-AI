"""A page that answers HTTP 200 but says it was not found is a missing page (moonsez.com/privacy and /terms), never a privacy policy that was found."""
import asyncio

import pytest

from app.scanner import crawler


class _Page:
    def __init__(self, title="", text="", boom=False):
        self._info, self._boom = {"title": title, "text": text}, boom

    async def evaluate(self, _script):
        if self._boom:
            raise RuntimeError("page closed")
        return self._info


def _says_missing(title="", text="", boom=False):
    return asyncio.run(crawler._page_says_it_was_not_found(_Page(title, text, boom)))


@pytest.mark.parametrize("title", ["Page Not Found.", "404 - Not Found", "404", "Error 404", "404 Error", "Oops, page not found", "Not Found", "The page cannot be found"])
def test_a_not_found_title_is_recognised(title):
    assert _says_missing(title, "a menu " * 100) is True


@pytest.mark.parametrize("title", ["Privacy Policy | Acme", "Terms of Use", "Contact us", "Foundation Terms", "Nothing found for your search", "404 Brewing Co. Privacy Policy", "Error handling policy"])
def test_a_real_page_title_is_not_taken_for_a_missing_page(title):
    assert _says_missing(title, "We collect your name and email to answer your enquiry. " * 40) is False


def test_the_moonsez_page_is_recognised_from_its_title_alone():
    """/privacy and /terms returned this: HTTP 200, title 'Page Not Found.', and a body that is only the site's menu and footer."""
    assert _says_missing("Page Not Found.", "Moonsez About Our Services Contact Us The page you are looking not be found. Moon SEZ Group of Companies " * 12) is True


def test_a_short_untitled_not_found_page_is_recognised_from_its_text():
    assert _says_missing("Acme", "Sorry, the page you requested could not be found.") is True
    assert _says_missing("Acme", "404") is True


def test_a_long_page_that_mentions_a_missing_page_in_passing_is_not_missing():
    text = "If the page you are looking for cannot be found, contact us. " + "Our privacy practices are described in full below. " * 60
    assert _says_missing("Privacy Policy", text) is False


def test_a_page_that_cannot_be_read_is_not_called_missing():
    assert _says_missing(boom=True) is False


# ---- the shared check, the policy reader, and the policy records ----
from app.scanner import soft_404                                          # noqa: E402
from app.policy import analysis                                            # noqa: E402

MOONSEZ_PAGE = "<html><head><title>Page Not Found.</title></head><body><nav>Home About Services Contact</nav><p>The page you are looking not be found.</p>" + "<p>Moon SEZ Group of Companies. </p>" * 60 + "</body></html>"


def test_the_shared_check_matches_the_crawler_check():
    assert soft_404.looks_not_found("Page Not Found.", "menu " * 300) is True
    assert soft_404.looks_not_found("Privacy Policy", "We collect your name. " * 100) is False
    assert soft_404.looks_not_found(None, None) is False


def test_the_policy_reader_does_not_read_a_not_found_page_as_a_privacy_notice(monkeypatch):
    class _Resp:
        status_code, text, content = 200, MOONSEZ_PAGE, MOONSEZ_PAGE.encode()
        headers = {"content-type": "text/html; charset=UTF-8"}
        is_redirect = False

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    async def safe(url): return None

    monkeypatch.setattr(analysis.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(analysis, "assert_safe_url", safe)
    assert asyncio.run(analysis.fetch_policy_text("https://moonsez.com/privacy")) is None


def test_a_real_short_policy_page_is_still_read(monkeypatch):
    real = "<html><head><title>Privacy Policy</title></head><body>" + "<p>We collect your name and email to reply to enquiries and keep it for one year.</p>" * 8 + "</body></html>"

    class _Resp:
        status_code, text, content = 200, real, real.encode()
        headers = {"content-type": "text/html"}
        is_redirect = False

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    async def safe(url): return None

    monkeypatch.setattr(analysis.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(analysis, "assert_safe_url", safe)
    assert "We collect your name" in (asyncio.run(analysis.fetch_policy_text("https://x.test/privacy")) or "")
