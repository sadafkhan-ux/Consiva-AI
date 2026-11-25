"""3.0.2: a single-page app whose server answers 404 for its own page addresses is read through the app, from the homepage (owner, 25 Nov 2026). A local test site, no network."""

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

INDEX = b"""<!doctype html><html><body><nav><a href="/">Home</a> <a href="/about">About</a></nav><main id=m>Home page text</main>
<script>
function render(){document.getElementById('m').textContent = location.pathname === '/about' ? 'About the book: a long page.' : (location.pathname === '/hidden' ? 'Hidden page content.' : 'Home page text');}
document.addEventListener('click', e => { const a = e.target.closest('a'); if (a) { e.preventDefault(); history.pushState({}, '', a.getAttribute('href')); render(); }});
window.addEventListener('popstate', render); render();
</script></body></html>"""


class _Spa(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        ok = self.path == "/"
        self.send_response(200 if ok else 404)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(INDEX if ok else b"<h1>404 - File or directory not found.</h1>")

    def log_message(self, *a):
        pass


def test_pages_of_a_single_page_app_are_reached_through_the_app_and_a_missing_page_is_not():
    pw = pytest.importorskip("playwright.async_api")
    from app.enterprise.crawl.inspect import _open_in_app

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Spa)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/"

    async def run():
        async with pw.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:                    # noqa: BLE001
                pytest.skip(f"no browser available: {exc}")
            page = await (await browser.new_context()).new_page()
            st = SimpleNamespace(scanner_timeout_seconds=20)
            out = {}
            for path in ("about", "hidden", "missing"):
                got = await _open_in_app(page, base + path, base, st)
                out[path] = (got[1] if got else None, await page.evaluate("document.body.innerText"))
            await browser.close()
            return out

    box: dict = {}

    def in_thread():                                    # its own thread and event loop: other tests may leave one set on the main thread
        # a loop that can start the browser process: another test may set a Windows selector policy, which cannot run subprocesses
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            box["out"] = loop.run_until_complete(run())
        except BaseException as exc:                    # noqa: BLE001
            box["err"] = exc
        finally:
            loop.close()
    t = threading.Thread(target=in_thread)
    t.start()
    t.join(120)
    srv.shutdown()
    if isinstance(box.get("err"), pytest.skip.Exception):
        raise box["err"]
    if "err" in box:
        raise box["err"]
    out = box["out"]
    assert out["about"][0] == "link" and "About the book" in out["about"][1]
    assert out["hidden"][0] == "router" and "Hidden page content" in out["hidden"][1]
    assert out["missing"][0] is None                   # a page the app does not have is still a failure
