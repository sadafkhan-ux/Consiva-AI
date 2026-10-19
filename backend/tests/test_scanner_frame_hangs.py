"""A child frame that never answers must cost seconds, not the scan.

Found on a real scan (smartians.ai, a <frameset> wrapper): the scan was aborted by its whole-scan timeout,
three attempts in a row, always at the same await -- `frame.content()` on one child frame. Playwright's
`Frame.content()` and `Locator.count()` have no timeout of their own, so one frame that never answers made
every later query to it wait until something above cancelled the lot.

Two layers here: fakes for the logic (fast, deterministic), and one real Chromium against a local server whose
frame never answers (skipped where no browser is installed).
"""

import asyncio
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from app.scanner import consent_interactor as ci
from app.scanner import crawler

pytestmark = pytest.mark.asyncio


# ── fakes ────────────────────────────────────────────────────────────────────────

class _Locator:
    def __init__(self, count_result=0, hang=False):
        self._count, self._hang = count_result, hang
        self.clicked = False

    async def count(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._count

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    async def click(self, timeout=None):
        self.clicked = True

    async def is_visible(self, timeout=None):
        return False

    async def inner_text(self, timeout=None):
        return ""


class _Frame:
    """`answers`: evaluate() works. `hangs`: evaluate() never returns. `detached`: it raises."""

    def __init__(self, url="https://x/", *, mode="answers", banner=False, count_hangs=False):
        self.url, self._mode, self._banner, self._count_hangs = url, mode, banner, count_hangs
        self.locator_calls = 0
        self._locator = _Locator(1 if banner else 0, hang=count_hangs)

    async def evaluate(self, script):
        if self._mode == "hangs":
            await asyncio.sleep(3600)
        if self._mode == "detached":
            raise PlaywrightError("Frame was detached")
        return 1

    def locator(self, selector):
        self.locator_calls += 1
        return self._locator


def _page(*frames):
    return SimpleNamespace(frames=list(frames), url="https://x/", wait_for_timeout=_noop)


async def _noop(_ms):
    return None


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    monkeypatch.setattr(ci, "_FRAME_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ci, "_FRAME_OP_TIMEOUT_SECONDS", 0.05)


# ── the probe ────────────────────────────────────────────────────────────────────

async def test_frames_that_hang_or_are_detached_are_separated_from_ones_that_answer():
    main, good, hung, gone = _Frame("main"), _Frame("good"), _Frame("", mode="hangs"), _Frame("about:blank", mode="detached")

    responsive, unresponsive = await asyncio.wait_for(ci._probe_frames(_page(main, good, hung, gone)), timeout=2)

    assert responsive == [main, good]
    assert unresponsive == [hung, gone]


async def test_several_hung_frames_cost_one_timeout_not_one_each():
    hung = [_Frame(f"h{i}", mode="hangs") for i in range(5)]

    started = time.monotonic()
    _responsive, unresponsive = await ci._probe_frames(_page(_Frame("main"), *hung))

    assert len(unresponsive) == 5
    assert time.monotonic() - started < 0.5, "probed together, so five hung frames take about one timeout"


async def test_the_page_itself_is_always_searched_even_when_it_has_no_children():
    main = _Frame("main")
    responsive, unresponsive = await ci._probe_frames(_page(main))
    assert responsive == [main] and unresponsive == []


# ── searching for a banner ───────────────────────────────────────────────────────

async def test_a_banner_is_still_found_in_a_good_frame_when_another_frame_hangs():
    hung, good = _Frame("", mode="hangs"), _Frame("https://cmp.example/", banner=True)

    result = await asyncio.wait_for(ci.click_reject(_page(_Frame("main"), hung, good)), timeout=5)

    assert result == "clicked"
    assert good._locator.clicked
    assert hung.locator_calls == 0, "the unresponsive frame was never queried"


async def test_a_frame_that_answered_the_probe_but_then_hangs_on_a_query_is_survived():
    stalls_later = _Frame("https://late.example/", count_hangs=True)

    result = await asyncio.wait_for(ci.click_accept(_page(_Frame("main"), stalls_later)), timeout=5)

    assert result == "not_found", "the hung query became an ordinary 'not usable' outcome, not a hang"


async def test_nothing_found_is_not_found_not_a_failure_when_every_child_frame_hangs():
    page = _page(_Frame("main"), _Frame("", mode="hangs"), _Frame("", mode="hangs"))
    assert await asyncio.wait_for(ci.click_reject(page), timeout=5) == "not_found"


# ── confirming that a click took effect ──────────────────────────────────────────

async def test_confirmation_is_withheld_when_a_frame_cannot_be_inspected():
    """A frame that will not answer might be the one still showing the banner, so it is 'unconfirmed' --
    never 'confirmed' on the strength of the frames that did answer."""
    page = _page(_Frame("main"), _Frame("", mode="hangs"))
    assert await asyncio.wait_for(ci.confirm_interaction_completed(page, accept=False), timeout=5) is False


async def test_confirmation_still_works_when_every_frame_answers_and_no_banner_remains():
    page = _page(_Frame("main"), _Frame("https://ok.example/"))
    assert await asyncio.wait_for(ci.confirm_interaction_completed(page, accept=False), timeout=5) is True


# ── the iframe reader in the crawler ─────────────────────────────────────────────

async def test_the_crawler_skips_a_frame_whose_content_never_arrives(monkeypatch):
    class _HungContent:
        url = ""

        async def content(self):
            await asyncio.sleep(3600)

    class _Readable:
        url = "https://widget.example/"

        async def content(self):
            return "<html><body><p>Manage cookies</p></body></html>"

    main = SimpleNamespace(url="https://x/")
    page = SimpleNamespace(main_frame=main, frames=[main, _HungContent(), _Readable()], url="https://x/")
    monkeypatch.setattr(crawler, "_IFRAME_READ_TIMEOUT_SECONDS", 0.05)

    _scripts, text, _controls = await asyncio.wait_for(crawler._collect_iframe_signal_evidence(page), timeout=5)

    assert "Manage cookies" in text


# ── a real browser, a real frameset, a frame that never answers ──────────────────

_FRAMESET = (
    b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">'
    b'<html><head><title>Wrapper</title></head><frameset rows="100%%,*"><frame src="%s"></frameset></html>'
)


class _Silent(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/silent":
            time.sleep(120)                      # never sends a byte
            return
        body = _FRAMESET % f"http://localhost:{self.server.server_port}/silent".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# Run in a clean child process: Playwright needs an event loop that can spawn the browser, and another test in
# a full run may have changed this process's loop policy (the same reason the scanner itself runs in a child on
# Windows). The child prints one line: NOBROWSER, or "OK <seconds> <outcome>".
_CHILD = r"""
import asyncio, sys, time
from playwright.async_api import Error as PlaywrightError, async_playwright
from app.scanner import consent_interactor as ci, crawler

async def main(url):
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except PlaywrightError:
            print("NOBROWSER"); return
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(500)
            if not [f for f in page.frames if f != page.main_frame]:
                print("NOFRAME"); return
            crawler._IFRAME_READ_TIMEOUT_SECONDS = 1
            ci._FRAME_PROBE_TIMEOUT_SECONDS = 1
            started = time.monotonic()
            await asyncio.wait_for(crawler._collect_iframe_signal_evidence(page), timeout=20)
            outcome = await asyncio.wait_for(ci.click_reject(page), timeout=20)
            print(f"OK {time.monotonic() - started:.1f} {outcome}")
        finally:
            await browser.close()

asyncio.run(main(sys.argv[1]))
"""


async def test_real_chromium_a_frameset_whose_frame_never_answers_does_not_hang_the_reader():
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip("playwright.async_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Silent)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend_root = Path(__file__).resolve().parent.parent
        done = subprocess.run(  # noqa: ASYNC221 -- one short child process; nothing else shares this loop
            [sys.executable, "-c", _CHILD, f"http://127.0.0.1:{server.server_port}/"],
            cwd=backend_root, env={**__import__("os").environ, "PYTHONPATH": str(backend_root)},
            capture_output=True, text=True, timeout=90, check=False,
        )
    finally:
        server.shutdown()
        socket.setdefaulttimeout(None)

    out = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr[-300:]
    if out == "NOBROWSER":
        pytest.skip("Chromium is not available in this environment")
    assert out.startswith("OK "), f"the reader did not finish: {out}"
    _ok, seconds, outcome = out.split()
    assert float(seconds) < 15, "both readers gave up on the silent frame quickly"
    assert outcome == "not_found"
