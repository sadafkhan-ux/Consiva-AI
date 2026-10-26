"""Consent detection reads the controls a visitor can SEE. A consent tool renders its second layer (the preference centre, with "Reject All Cookies") into the page hidden; reading it made
a banner whose first layer is "Accept All" + "Cookie Settings" look as if it offered Reject up front (seen live on konicaminolta.com, OneTrust)."""

from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.crawler import _SHADOW_DOM_CONTROL_TEXT_JS
from tests._browser import run_with_browser
from tests.test_report_pdf_api import needs_chromium

ONETRUST_LIKE = """
<div id="onetrust-banner-sdk" style="position:fixed;bottom:0;left:0;width:100%;height:80px">
  <button>Accept All Cookies</button><button>Cookie Settings</button>
</div>
<div id="onetrust-pc-sdk" style="display:none"><button>Reject All Cookies</button><button>Save Settings</button><button>Confirm My Choices</button></div>
<footer><a href="/x" style="display:none">Hidden link</a><a href="/y">Contact</a></footer>
"""


def control_text(html: str) -> str:
    """What the scanner's control-text script reads from a page with this body, in a real browser."""
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await (await browser.new_context()).new_page()
            await page.set_content(html)
            text = await page.evaluate(_SHADOW_DOM_CONTROL_TEXT_JS)
            await browser.close()
            return text.lower()
    return run_with_browser(go)


@needs_chromium
class TestTheControlsAVisitorCanSee:
    def test_a_hidden_second_layer_is_not_read(self):
        t = control_text(ONETRUST_LIKE)
        assert "accept all cookies" in t and "cookie settings" in t and "contact" in t
        assert "reject all cookies" not in t and "save settings" not in t and "hidden link" not in t

    def test_visibility_hidden_and_zero_size_controls_are_not_read(self):
        t = control_text('<button style="visibility:hidden">Reject all</button><button style="width:0;height:0;padding:0;border:0;overflow:hidden"></button><button>Accept all</button>')
        assert "reject all" not in t and "accept all" in t

    def test_a_reject_on_the_first_layer_is_read(self):
        assert "reject all" in control_text("<div><button>Accept all</button><button>Reject all</button></div>")

    def test_the_banner_is_then_recorded_without_a_first_layer_reject(self):
        sig = detect_consent_signal(scripts=[], visible_text="", detected_global_vars=[], control_text=control_text(ONETRUST_LIKE))
        assert sig.mechanism_type == "banner" and sig.has_reject_all is False, "Reject exists only in the closed preference centre"

    def test_an_open_shadow_root_is_still_read(self):
        html = "<div id='h'></div><script>const r=document.getElementById('h').attachShadow({mode:'open'});r.innerHTML='<button>Accept all</button><button>Reject all</button>';</script>"
        t = control_text(html)
        assert "accept all" in t and "reject all" in t


def test_the_detector_still_reads_whatever_text_it_is_given_without_a_browser():
    sig = detect_consent_signal(scripts=[], visible_text="", detected_global_vars=[], control_text="accept all reject all")
    assert sig.has_reject_all is True
