"""Scanning a site that uses the Consiva Consent SDK (found on gignaati.com).

Three things were wrong, each found on the live site:

  * The SDK shows an AGE GATE ("Yes, I am 18+") before the cookie banner, so the scanner found neither Accept
    nor Reject and reported both as "not automatable". The gate is now catalogued and answered -- and recorded.
  * The SDK blocks a tracker tag by rewriting it to type="text/plain". The tag stays in the DOM but never runs;
    counting it as a tracker that "fired before consent" was a false positive.
  * Consiva's own Accept/Reject controls were not catalogued.

Fakes for the logic; one real Chromium against a page that rebuilds the SDK's structure (open shadow root, a first
host with the age gate, a second with the modal banner) for the browser behaviour.
"""

import asyncio
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from app.rules.tracker_catalog import CMP_CATALOG, match_cmp
from app.scanner import consent_interactor as ci
from app.scanner import crawler
from app.scanner.page_parser import parse_page, script_executes
from app.scanner.tracker_detector import detect_trackers

CONSIVA = next(c for c in CMP_CATALOG if c.cmp_vendor == "Consiva Consent SDK")


# ── the catalogue ────────────────────────────────────────────────────────────────

def test_consiva_is_catalogued_with_its_real_controls_and_its_age_gate():
    assert CONSIVA.accept_selector == "#cmp-accept-all"
    assert CONSIVA.reject_selector == "#cmp-reject-all"
    assert CONSIVA.gate_selector == "#cmp-age-yes"
    assert CONSIVA.gate_kind == "age_gate"


def test_the_sdk_script_is_still_recognised_as_the_consiva_cmp():
    assert match_cmp(script_src="https://api.consiva.ai/sdk.js").cmp_vendor == "Consiva Consent SDK"
    assert match_cmp(script_src="https://uatapi.consiva.ai/sdk.js").cmp_vendor == "Consiva Consent SDK"


def test_no_other_cmp_gains_a_gate_by_accident():
    assert [c.cmp_vendor for c in CMP_CATALOG if c.gate_selector] == ["Consiva Consent SDK"]


# ── a blocked tag is not a tracker that fired ────────────────────────────────────

@pytest.mark.parametrize("type_attr,runs", [
    (None, True), ("", True), ("text/javascript", True), ("application/javascript", True), ("module", True),
    ("text/javascript; charset=utf-8", True), ("TEXT/JAVASCRIPT", True),
    ("text/plain", False), ("text/template", False), ("application/json", False), ("text/x-handlebars", False),
])
def test_only_script_types_a_browser_runs_count_as_executing(type_attr, runs):
    assert script_executes(type_attr) is runs


GTM = "https://www.googletagmanager.com/gtag/js?id=G-ES6VZESDMQ"


def _html(type_attr):
    attr = f' type="{type_attr}"' if type_attr else ""
    return f'<html><body><script{attr} src="{GTM}" data-cookiecategory="statistics"></script></body></html>'


def test_a_cmp_blocked_tag_is_not_recorded_as_a_tracker():
    parsed = parse_page(_html("text/plain"), "https://gignaati.com/")

    assert parsed.scripts[0].src == GTM and parsed.scripts[0].executes is False
    records = detect_trackers(site_domain="gignaati.com", scripts_by_page={"p": parsed.scripts}, network_requests_by_page={})
    assert records == []


def test_the_same_tag_once_it_runs_is_recorded():
    parsed = parse_page(_html("text/javascript"), "https://gignaati.com/")

    records = detect_trackers(site_domain="gignaati.com", scripts_by_page={"p": parsed.scripts}, network_requests_by_page={})

    assert [r.script_src for r in records] == [GTM]


def test_a_real_network_request_is_a_tracker_even_if_the_tag_looks_blocked():
    """The tag says text/plain, but the browser DID call out: that request is what fired."""
    parsed = parse_page(_html("text/plain"), "https://gignaati.com/")

    records = detect_trackers(
        site_domain="gignaati.com", scripts_by_page={"p": parsed.scripts},
        network_requests_by_page={"p": [(GTM, "script")]},
    )

    assert [r.script_src for r in records] == [GTM]


def test_the_cmp_script_itself_still_counts_for_cmp_detection():
    html = '<html><script src="https://api.consiva.ai/sdk.js" data-domain-id="x"></script></html>'
    scripts = parse_page(html, "https://gignaati.com/").scripts
    assert scripts[0].executes is True and "consiva.ai" in scripts[0].src


# ── the gate, with fakes ─────────────────────────────────────────────────────────

class _Locator:
    def __init__(self, count=1, visible=True, click_raises=False, appears=True):
        self._count, self._visible, self._click_raises, self._appears = count, visible, click_raises, appears
        self.clicked = False

    async def count(self):
        return self._count

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None):
        return self._visible

    async def click(self, timeout=None):
        if self._click_raises:
            raise PlaywrightError("element is covered")
        self.clicked = True

    async def wait_for(self, state="visible", timeout=None):
        if not self._appears:
            raise PlaywrightError("Timeout waiting for the banner")


class _Frame:
    def __init__(self, selectors):
        self.url = "https://gignaati.com/"
        self._selectors = selectors

    def locator(self, selector):
        return self._selectors.get(selector, _Locator(count=0))

    async def evaluate(self, script):
        return 1


def _page(selectors):
    return SimpleNamespace(frames=[_Frame(selectors)], url="https://gignaati.com/")


async def test_a_gate_on_the_page_is_answered_and_the_banner_it_unlocks_is_awaited():
    gate = _Locator()
    page = _page({"#cmp-age-yes": gate, "#cmp-accept-all": _Locator()})

    record = await ci.pass_consent_gate(page)

    assert gate.clicked
    assert record == {"kind": "age_gate", "cmp": "Consiva Consent SDK", "answer": "adult", "outcome": "passed"}


async def test_no_gate_means_nothing_is_clicked_and_no_record_is_made():
    assert await ci.pass_consent_gate(_page({"#cmp-accept-all": _Locator()})) is None


async def test_a_gate_that_is_not_visible_is_left_alone():
    gate = _Locator(visible=False)
    assert await ci.pass_consent_gate(_page({"#cmp-age-yes": gate})) is None
    assert not gate.clicked


async def test_a_gate_that_cannot_be_clicked_is_reported_not_hidden():
    record = await ci.pass_consent_gate(_page({"#cmp-age-yes": _Locator(click_raises=True)}))
    assert record["outcome"] == "click_failed" and record["answer"] == "adult"


async def test_a_gate_answered_but_no_banner_following_is_reported_as_such():
    page = _page({"#cmp-age-yes": _Locator(), "#cmp-accept-all": _Locator(appears=False)})
    record = await ci.pass_consent_gate(page)
    assert record["outcome"] == "banner_not_shown_after_gate"


async def test_the_catalogued_consiva_controls_are_what_accept_and_reject_click():
    accept, reject = _Locator(), _Locator()
    page = _page({"#cmp-accept-all": accept, "#cmp-reject-all": reject})

    assert await ci.click_reject(page) == "clicked"
    assert reject.clicked and not accept.clicked
    assert await ci.click_accept(page) == "clicked"
    assert accept.clicked


# ── the pass carries the gate result ─────────────────────────────────────────────

async def test_the_interaction_pass_returns_the_gate_record_and_still_matches_its_annotation(monkeypatch):
    from tests.test_scanner_coverage import ROOT, _FakeContext, _links

    record = {"kind": "age_gate", "cmp": "Consiva Consent SDK", "answer": "adult", "outcome": "passed"}
    monkeypatch.setattr(crawler, "pass_consent_gate", lambda page: _async_value(record))
    monkeypatch.setattr(crawler, "confirm_interaction_completed", lambda page, accept: _async_value(True))

    async def click(_page):
        return "clicked"

    from app.config import get_settings
    context = _FakeContext({f"{ROOT}/": _links()})
    out = await crawler._crawl_single_page_with_interaction(
        context, f"{ROOT}/", get_settings(), set(), "post_reject", click,
    )

    assert len(out) == 6
    assert out[2] == "clicked" and out[3] is True and out[5] == record


async def test_a_robots_disallowed_page_returns_the_same_shape_as_every_other_outcome():
    from app.config import get_settings
    out = await crawler._crawl_single_page_with_interaction(
        SimpleNamespace(), "https://x.example/private", get_settings(), {"/private"}, "post_accept", None,
    )
    assert len(out) == 6 and out[2] == "page_unreachable" and out[5] is None


async def _async_value(value):
    return value


# ── "no reject-all option" must not contradict a Reject the scanner clicked ──────

def _signal(has_reject_all):
    from app.scanner.schemas import ConsentSignalRecord
    return ConsentSignalRecord(mechanism_type="cmp", cmp_vendor="Consiva Consent SDK", has_reject_all=has_reject_all)


def test_a_reject_control_that_was_clicked_and_confirmed_counts_as_present():
    signal = _signal(False)

    crawler._reconcile_reject_presence(signal, "clicked", True)

    assert signal.has_reject_all is True
    assert signal.evidence["reject_control_seen_via"] == "interaction_pass"


@pytest.mark.parametrize("status,confirmed", [
    ("clicked", False),          # dispatched, but the banner never observably responded
    ("click_failed", False), ("not_found", False), ("page_unreachable", False),
    ("click_failed", True),
])
def test_anything_short_of_a_confirmed_click_leaves_the_page_text_verdict_alone(status, confirmed):
    signal = _signal(False)

    crawler._reconcile_reject_presence(signal, status, confirmed)

    assert signal.has_reject_all is False and "reject_control_seen_via" not in signal.evidence


@pytest.mark.parametrize("before", [True, None])
def test_a_verdict_other_than_false_is_never_touched(before):
    signal = _signal(before)
    crawler._reconcile_reject_presence(signal, "clicked", True)
    assert signal.has_reject_all is before


def _evidence(has_reject_all):
    return {
        "cookies": [], "trackers": [], "forms": [], "policies": [{"policy_type": "privacy_policy"}],
        "consent_signals": [{"mechanism_type": "cmp", "has_reject_all": has_reject_all,
                             "evidence": {"accept_interaction": "clicked", "reject_interaction": "clicked", "reject_click_confirmed": True}}],
    }


def test_r005_no_longer_fires_for_a_banner_whose_reject_was_found_and_clicked():
    from app.rules.consent_rules import evaluate_consent_rules

    signal = _signal(False)
    crawler._reconcile_reject_presence(signal, "clicked", True)

    ids = [f.rule_id for f in evaluate_consent_rules(_evidence(signal.has_reject_all))]
    assert "R-005" not in ids
    # ...and the rule still fires when the page genuinely has no reject option and nothing was clicked.
    assert "R-005" in [f.rule_id for f in evaluate_consent_rules(_evidence(False))]


# ── a real browser against a page shaped like the SDK ────────────────────────────

_PAGE = b"""<!doctype html><html><head><title>t</title></head><body><h1>Site</h1><script>
(function () {
  function host() { const h = document.createElement('div'); h.id = 'cmp-host'; h.attachShadow({mode: 'open'}); document.body.appendChild(h); return h; }
  function btn(root, id, text, onclick) { const b = document.createElement('button'); b.id = id; b.textContent = text; b.onclick = onclick; root.appendChild(b); }
  const gate = host(); const gateRoot = gate.shadowRoot;
  btn(gateRoot, 'cmp-age-no', 'No, I am under 18', () => {});
  btn(gateRoot, 'cmp-age-yes', 'Yes, I am 18+', () => {
    gateRoot.innerHTML = '';                                   // the gate host stays, EMPTY, like the real SDK
    setTimeout(() => {
      const banner = host(); const root = banner.shadowRoot;   // a SECOND #cmp-host holds the banner
      const dlg = document.createElement('div'); dlg.setAttribute('role', 'dialog'); dlg.setAttribute('aria-modal', 'true'); root.appendChild(dlg);
      btn(dlg, 'cmp-accept-all', 'Accept All', () => { window.__consent = 'accept'; document.cookie = 'analytics=1; path=/'; banner.remove(); });
      btn(dlg, 'cmp-reject-all', 'Reject All', () => { window.__consent = 'reject'; banner.remove(); });
      btn(dlg, 'cmp-preferences-btn', 'Preferences', () => {});
    }, 300);
  });
})();
</script></body></html>"""


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)


_CHILD = r"""
import asyncio, sys
from playwright.async_api import Error as PlaywrightError, async_playwright
from app.scanner import consent_interactor as ci

async def run(browser, url, which):
    page = await (await browser.new_context()).new_page()
    await page.goto(url, wait_until="load", timeout=15000)
    before = await ci.click_reject(page) if which == "reject" else await ci.click_accept(page)   # no gate pass first
    page2 = await (await browser.new_context()).new_page()
    await page2.goto(url, wait_until="load", timeout=15000)
    gate = await ci.pass_consent_gate(page2)
    click = await (ci.click_reject if which == "reject" else ci.click_accept)(page2)
    await page2.wait_for_timeout(500)
    confirmed = await ci.confirm_interaction_completed(page2, accept=(which == "accept"))
    consent = await page2.evaluate("window.__consent || null")
    cookie = await page2.evaluate("document.cookie")
    return f"{which}|before_gate={before}|gate={gate['outcome'] if gate else None}|click={click}|confirmed={confirmed}|consent={consent}|cookie={cookie}"

async def main(url):
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except PlaywrightError:
            print("NOBROWSER"); return
        try:
            for which in ("reject", "accept"):
                print(await run(browser, url, which))
        finally:
            await browser.close()

asyncio.run(main(sys.argv[1]))
"""


async def test_real_chromium_the_age_gate_is_passed_and_accept_and_reject_really_work():
    pytest.importorskip("playwright.async_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend_root = Path(__file__).resolve().parent.parent
        env = {**__import__("os").environ, "PYTHONPATH": str(backend_root)}
        done = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", _CHILD, f"http://127.0.0.1:{server.server_port}/"],
            cwd=backend_root, env=env, capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        server.shutdown()

    lines = [ln for ln in done.stdout.strip().splitlines() if ln]
    if lines and lines[0] == "NOBROWSER":
        pytest.skip("Chromium is not available in this environment")
    assert len(lines) == 2, done.stdout + done.stderr[-400:]
    reject, accept = (dict(p.split("=", 1) if "=" in p else (p, "") for p in ln.split("|")) for ln in lines)

    # Without passing the gate first, neither control exists -- the exact symptom seen on gignaati.com.
    assert reject["before_gate"] == "not_found" and accept["before_gate"] == "not_found"
    # With the gate passed, the real controls are found, clicked, and the SDK recorded the choice.
    assert reject["gate"] == "passed" and reject["click"] == "clicked"
    assert reject["confirmed"] == "True" and reject["consent"] == "reject" and "analytics" not in reject["cookie"]
    assert accept["gate"] == "passed" and accept["click"] == "clicked"
    assert accept["confirmed"] == "True" and accept["consent"] == "accept" and "analytics=1" in accept["cookie"]


def test_the_interaction_page_is_recorded_in_the_consent_signal_evidence():
    import inspect

    from app.scanner import crawler

    assert '"interaction_page_url": root_url' in inspect.getsource(crawler.run_scan)


# ── a banner that appears only after the page's load event ──────────────────────────
# _navigate_with_retry returns at "domcontentloaded". The interaction passes must still wait for the banner's own
# script, which runs later. Seen on masterclass.gignaati.com after merging the faster navigation: Accept and Reject
# went from "clicked" to "cmp_not_automatable".

_LATE_PAGE = b"""<!doctype html><html><head><title>t</title></head><body><h1>Site</h1>
<img src="/slow.png" width="1" height="1">
<script>
window.addEventListener('load', function () {
  const b = document.createElement('div'); b.id = 'late-banner';
  b.innerHTML = '<button id="onetrust-accept-btn-handler">Accept All Cookies</button>'
              + '<button id="onetrust-reject-all-handler">Reject All</button>';
  document.body.appendChild(b);
  document.getElementById('onetrust-accept-btn-handler').onclick = function () { window.__consent = 'accept'; b.remove(); };
  document.getElementById('onetrust-reject-all-handler').onclick = function () { window.__consent = 'reject'; b.remove(); };
});
</script></body></html>"""


class _LateSite(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/slow.png"):
            time.sleep(3.5)                   # holds the load event back well past the settle window
            body, kind = b"", "image/png"
        else:
            body, kind = _LATE_PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_LATE_CHILD = r"""
import asyncio, sys
from playwright.async_api import Error as PlaywrightError, async_playwright
from app.config import get_settings
from app.scanner import consent_interactor as ci, crawler

async def main(url):
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except PlaywrightError:
            print("NOBROWSER"); return
        try:
            context = await browser.new_context()
            *_, status, confirmed, _steps, _gate = await crawler._crawl_single_page_with_interaction(
                context, url, get_settings(), set(), "post_reject", ci.click_reject)
            print(f"status={status}|confirmed={confirmed}")
        finally:
            await browser.close()

asyncio.run(main(sys.argv[1]))
"""


async def test_real_chromium_the_interaction_waits_for_a_banner_that_appears_after_load():
    pytest.importorskip("playwright.async_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LateSite)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend_root = Path(__file__).resolve().parent.parent
        env = {**__import__("os").environ, "PYTHONPATH": str(backend_root)}
        done = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", _LATE_CHILD, f"http://127.0.0.1:{server.server_port}/"],
            cwd=backend_root, env=env, capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        server.shutdown()

    out = done.stdout.strip()
    if out == "NOBROWSER":
        pytest.skip("Chromium is not available in this environment")
    assert "status=clicked|confirmed=True" in out, out + done.stderr[-500:]
