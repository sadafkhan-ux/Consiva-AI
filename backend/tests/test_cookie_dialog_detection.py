"""A cookie notice whose buttons say only "Accept" and "Decline" (seen on greenply.com) is recognised by structure; ordinary pages with the same words are not."""
import pytest
from bs4 import BeautifulSoup

from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.page_parser import cookie_dialog_controls, extract_control_text

GREENPLY = """<html><body><h1>Doors of India</h1><p>Both parties agree on this.</p>
<div class="cookie-popup"><p>We use cookies to personalise and enhance your browsing experience on our website. Click 'Accept' to get the most out of our site!</p>
<button>Accept</button><button class="decline">Decline</button></div></body></html>"""


def signal(html: str):
    text = extract_control_text(BeautifulSoup(html, "html.parser"))
    return detect_consent_signal(scripts=[], visible_text="", detected_global_vars=[], control_text=text)


def test_a_bare_accept_and_decline_cookie_popup_is_a_banner():
    s = signal(GREENPLY)
    assert s.mechanism_type == "banner" and s.has_reject_all is True
    assert "cookie-dialog-accept" in s.evidence["matched_keywords"]


def test_accept_alone_is_still_a_banner_without_a_reject_control():
    s = signal(GREENPLY.replace('<button class="decline">Decline</button>', ""))
    assert s.mechanism_type == "banner" and s.has_reject_all is False


def test_a_forum_with_the_word_agree_and_no_cookie_dialog_is_not_a_banner():
    assert signal("<html><body><p>both parties agree on this</p><a href='/x'>OK</a><button>Accept</button></body></html>").mechanism_type == "none"


def test_a_button_outside_a_cookie_dialog_does_not_count():
    assert cookie_dialog_controls(BeautifulSoup("<div class='hero'>We use cookies in baking.<button>Accept</button></div>", "html.parser")) == (False, False)


def test_a_long_page_container_named_as_consent_is_not_a_dialog():
    long_text = "We use cookies. " * 80
    html = f"<div class='consent-page'>{long_text}<button>Accept</button></div>"
    assert cookie_dialog_controls(BeautifulSoup(html, "html.parser")) == (False, False)


def test_a_dialog_that_never_mentions_cookies_does_not_count():
    assert cookie_dialog_controls(BeautifulSoup("<div class='consent-form'>Please confirm your newsletter signup now.<button>Accept</button></div>", "html.parser")) == (False, False)


def test_the_live_page_script_carries_the_same_structural_rule():
    """The crawler reads the live DOM (including shadow roots) through this script, not page_parser; both must carry the rule."""
    from app.scanner import crawler

    js = crawler._SHADOW_DOM_CONTROL_TEXT_JS
    assert "cookie-dialog-accept" in js and "cookie-dialog-reject" in js and "BARE_ACCEPT" in js and "text.length > 700" in js


@pytest.mark.asyncio
async def test_the_accept_button_is_found_however_many_other_buttons_come_before_it():
    """greenply.com: the cookie popup's Accept button sits after hundreds of other buttons; only the first 50 controls were read, so Accept was 'not found'."""
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    from app.scanner import consent_interactor as ci

    html = "<html><body>" + "".join(f"<button>Item {i}</button>" for i in range(300)) + "<div class='cookie-popup'>We use cookies.<button>Accept</button><button>Decline</button></div></body></html>"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            accept, reject = await ci.click_accept(page), await ci.click_reject(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 -- no browser installed here
        pytest.skip(f"no browser: {exc}")
    assert accept == "clicked" and reject == "clicked"
