"""Large photographs on the pages after the first are not downloaded; nothing a tracker could be is ever skipped, and the first page is untouched."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.scanner.image_blocking import should_skip_image
from tests._browser import run_with_browser
from tests.test_report_pdf_api import needs_chromium

SITE = "example.com"


class TestWhichImagesAreSkipped:
    @pytest.mark.parametrize("url", [
        "https://www.example.com/media/hero.jpg",
        "https://www.example.com/media/hero.webp?w=1200",       # first-party with a query: a first party is not a tracker
        "https://cdn.shopify.net/files/product-1.png",           # a content network, no query
        "https://images.example.com/a/b/c.avif",
    ])
    def test_plain_photographs_are_skipped(self, url):
        assert should_skip_image(url, "image", SITE) is True

    @pytest.mark.parametrize("url", [
        "https://www.facebook.com/tr?id=123&ev=PageView",                 # known tracker
        "https://www.facebook.com/tr/photo.png",                          # known tracker even with an image extension
        "https://stats.g.doubleclick.net/collect.png",
        "https://px.unknown-adtech.net/p.png?uid=77",                     # third party with a query: the shape of a pixel
        "https://www.example.com/pixel.png",                              # beacon word
        "https://cdn.other.net/track/impression.jpg",
        "https://www.example.com/spacer.gif",                             # gif is never a photograph here
        "https://www.example.com/logo.svg",
        "https://www.example.com/image-with-no-extension",
        "data:image/png;base64,AAAA",
    ])
    def test_anything_a_tracker_could_be_is_kept(self, url):
        assert should_skip_image(url, "image", SITE) is False

    @pytest.mark.parametrize("rtype", ["script", "xhr", "fetch", "document", "stylesheet", "font", "media", "ping"])
    def test_only_image_requests_are_ever_skipped(self, rtype):
        assert should_skip_image("https://www.example.com/hero.jpg", rtype, SITE) is False


PAGE = """<html><body><h1>Page</h1>
<img src="/photo.jpg"><img src="https://www.facebook.com/tr?id=1&ev=PageView"><img src="https://px.unknown-adtech.net/p.png?uid=7">
<img src="https://cdn.other.net/big.png"><img src="/pixel.png"><img src="/logo.svg"></body></html>"""
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
HITS: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        HITS.append(self.path)
        if self.path in ("/a", "/b"):
            body, ctype = PAGE.encode(), "text/html"
        else:
            body, ctype = GIF, "image/gif"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


@needs_chromium
def test_a_real_browser_secondary_page_skips_only_the_photographs():
    from playwright.async_api import async_playwright

    from app.config import get_settings
    from app.scanner.crawler import _fetch_one_page

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    settings = get_settings()
    outside_hits: list[str] = []

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context()

            async def external(route):
                if route.request.url.startswith(base):
                    await route.continue_()
                    return
                outside_hits.append(route.request.url)
                await route.fulfill(status=200, content_type="image/gif", body=GIF)

            await context.route("**/*", external)
            try:
                seed = await _fetch_one_page(context, f"{base}/a", f"{base}/a", settings)
                seed_hits, seed_outside = list(HITS), list(outside_hits)
                HITS.clear(); outside_hits.clear()
                second = await _fetch_one_page(context, f"{base}/b", f"{base}/a", settings)
                return seed, seed_hits, seed_outside, second, list(HITS), list(outside_hits)
            finally:
                await context.close(); await browser.close()

    try:
        seed, seed_hits, seed_outside, second, second_hits, second_outside = run_with_browser(go)
    finally:
        server.shutdown()

    # the first page: every image is fetched, as before
    assert "/photo.jpg" in seed_hits and "/pixel.png" in seed_hits
    assert any("cdn.other.net/big.png" in u for u in seed_outside)

    # a later page: the photographs are not fetched ...
    assert "/photo.jpg" not in second_hits
    assert not any("cdn.other.net/big.png" in u for u in second_outside)
    # ... but the tracker pixels, the beacon-like image and the svg are
    assert any("facebook.com/tr?id=1" in u for u in second_outside)
    assert any("px.unknown-adtech.net/p.png?uid=7" in u for u in second_outside)
    assert "/pixel.png" in second_hits and "/logo.svg" in second_hits

    # and the scan still records every request the page made, skipped or not, so tracker detection sees the same list
    seed_urls = {u.replace(f":{base.rsplit(':', 1)[1]}", "") for u, _ in seed["request_records"]}
    second_urls = {u.replace(f":{base.rsplit(':', 1)[1]}", "") for u, _ in second["request_records"]}
    assert seed_urls - {"http://127.0.0.1/a"} == second_urls - {"http://127.0.0.1/b"}
