"""The homepage preview: taken once per scan, harmless to the scan, kept with it, and shown honestly when it cannot be taken."""

import asyncio
import base64
import os
import re
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.api.v1.routes import consent_scans
from app.db.models import ScanScreenshot
from app.db.repositories import scan_repository
from app.main import app
from app.scanner import crawler
from app.scanner.schemas import ConsentSignalRecord, ScanResult, ScreenshotRecord

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 6000 + b"\xff\xd9"          # large enough to count as a picture


# ── a stand-in page that records everything it is asked to do ────────────────────────────────────────────────────────────────────

class FakePage:
    def __init__(self, title="Acme Corp | Home", shots=(JPEG,), shot_error=None, title_error=None, viewport=None):
        self._title, self._shots, self._shot_error, self._title_error = title, list(shots), shot_error, title_error
        self.viewport_size = viewport if viewport is not None else {"width": 1280, "height": 720}
        self.calls: list[tuple] = []

    async def title(self):
        self.calls.append(("title",))
        if self._title_error:
            raise self._title_error
        return self._title

    async def screenshot(self, **kw):
        self.calls.append(("screenshot", kw.get("quality"), kw.get("full_page"), kw.get("type")))
        if self._shot_error:
            raise self._shot_error
        return self._shots.pop(0) if len(self._shots) > 1 else self._shots[0]


def run(coro):
    return asyncio.run(coro)


def capture(page, status=200, url="https://acme.test/"):
    return run(crawler._capture_homepage_screenshot(page, http_status=status, final_url=url))


# ── capture ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_loaded_homepage_gives_a_jpeg_with_its_size_and_where_it_came_from():
    out = capture(FakePage())
    assert out["status"] == "ok" and out["reason"] is None
    assert base64.b64decode(out["image_b64"]) == JPEG
    assert (out["width"], out["height"]) == (1280, 720)
    assert out["page_url"] == "https://acme.test/" and out["http_status"] == 200 and out["page_title"] == "Acme Corp | Home"


def test_the_first_screen_only_is_photographed_as_a_jpeg():
    page = FakePage()
    capture(page)
    assert page.calls == [("title",), ("screenshot", crawler._SHOT_QUALITY, False, "jpeg")]   # no full page, nothing else touched


def test_taking_the_picture_does_nothing_else_to_the_page():
    """The fake offers only title() and screenshot(): a click, scroll, evaluate or navigation would fail on it and show up as a failed capture."""
    out = capture(FakePage())
    assert out["status"] == "ok"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_an_error_answer_is_not_the_clients_website_and_is_not_kept(status):
    page = FakePage()
    out = capture(page, status=status)
    assert out["status"] == "blocked" and out["reason"] == "blocked" and "image_b64" not in out
    assert ("screenshot", crawler._SHOT_QUALITY, False, "jpeg") not in page.calls          # no picture of a block page is even taken


@pytest.mark.parametrize("title", ["Access Denied", "403 Forbidden", "Just a moment...", "Attention Required! | Cloudflare", "Security Check", "Request blocked",
                                   "Are you a robot?", "Please complete the CAPTCHA", "502 Bad Gateway", "404 Not Found"])
def test_a_block_challenge_or_error_page_is_recognised_by_its_title(title):
    out = capture(FakePage(title=title))
    assert out["status"] == "blocked" and "image_b64" not in out


@pytest.mark.parametrize("title", ["Apollo Hospitals | Best Hospital in India", "Acme Corp | Home", "Contact 404 Park Street", "Forbidden City Tours", "Blocked Drains London", "Error Monitoring Platform | Sentry", "Captcha Solutions Inc"])
def test_a_real_homepage_is_not_mistaken_for_a_block_page(title):
    """Words that merely appear in an ordinary title must not discard the client's real picture."""
    out = capture(FakePage(title=title))
    assert out["status"] == "ok", title


def test_a_blank_page_is_not_a_preview():
    out = capture(FakePage(shots=(b"\xff\xd8" + b"\x00" * 200,)))
    assert out["status"] == "failed" and out["reason"] == "blank" and "image_b64" not in out


def test_an_oversized_picture_is_retaken_smaller():
    page = FakePage(shots=(b"\x00" * (crawler._SHOT_MAX_BYTES + 1), JPEG))
    out = capture(page)
    assert out["status"] == "ok" and base64.b64decode(out["image_b64"]) == JPEG
    qualities = [c[1] for c in page.calls if c[0] == "screenshot"]
    assert qualities == [crawler._SHOT_QUALITY, crawler._SHOT_RETRY_QUALITY]


def test_a_timeout_is_a_status_not_an_exception():
    out = capture(FakePage(shot_error=PlaywrightTimeoutError("screenshot timed out")))
    assert out["status"] == "timeout" and out["reason"] == "capture_timeout"


@pytest.mark.parametrize("error", [PlaywrightError("target closed"), RuntimeError("boom"), ValueError("bad"), OSError("disk")])
def test_any_other_failure_is_a_status_not_an_exception(error):
    out = capture(FakePage(shot_error=error))
    assert out["status"] == "failed" and out["reason"] == "capture_failed"
    assert "boom" not in str(out) and "target closed" not in str(out)      # nothing internal is carried forward


def test_a_page_that_will_not_give_its_title_is_still_photographed():
    out = capture(FakePage(title_error=PlaywrightError("navigating")))
    assert out["status"] == "ok" and out["page_title"] == ""


def test_a_page_with_no_viewport_size_falls_back_to_the_default():
    out = capture(_NoViewport())
    assert out["status"] == "ok" and (out["width"], out["height"]) == (1280, 720)


class _NoViewport(FakePage):
    def __init__(self):
        super().__init__()
        self.viewport_size = None


# ── a real browser: capturing changes nothing about the page or the browser ─────────────────────────────────────────────────────────

PAGE = b"""<!doctype html><html><head><title>Real Site | Home</title><style>body{margin:0;font:32px sans-serif}
.hero{height:500px;background:linear-gradient(135deg,#1c3faa,#e84393);color:#fff;padding:40px}.tall{height:3000px;background:#eee}</style></head>
<body><div class="hero"><h1>Welcome to Real Site</h1><p>We make real things.</p></div><div class="tall">more</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        if self.path == "/blocked":
            self.send_response(403)
            body = b"<html><head><title>Access Denied</title></head><body>no</body></html>"
        else:
            self.send_response(200)
            body = PAGE
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.mark.asyncio
async def test_a_real_browser_takes_a_real_picture_and_leaves_the_page_and_browser_untouched(server):
    from playwright.async_api import async_playwright

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:  # no browser installed here
        pytest.skip(f"no Chromium available: {exc}")
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await context.add_cookies([{"name": "keep", "value": "me", "url": server}])
        resp = await page.goto(server + "/", wait_until="domcontentloaded")
        await page.evaluate("localStorage.setItem('k', 'v')")
        before = (await context.cookies(), await page.evaluate("Object.assign({}, localStorage)"), await page.evaluate("window.scrollY"), page.url)
        hits_before = list(_Handler.hits)

        out = await crawler._capture_homepage_screenshot(page, http_status=resp.status, final_url=page.url)

        after = (await context.cookies(), await page.evaluate("Object.assign({}, localStorage)"), await page.evaluate("window.scrollY"), page.url)
        assert out["status"] == "ok"
        data = base64.b64decode(out["image_b64"])
        assert data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9"                      # a JPEG
        assert 2_500 < len(data) <= crawler._SHOT_MAX_BYTES
        assert (out["width"], out["height"]) == (1280, 720) and out["page_title"] == "Real Site | Home"
        assert before == after                                                                # cookies, storage, scroll position, URL: all unchanged
        assert _Handler.hits == hits_before                                                   # and no extra request was made to the site
        # it is the FIRST screen, not the whole page
        assert (await page.evaluate("document.documentElement.scrollHeight")) > 3000
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_a_real_browser_does_not_keep_a_block_page(server):
    from playwright.async_api import async_playwright

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = await (await browser.new_context()).new_page()
        resp = await page.goto(server + "/blocked", wait_until="domcontentloaded")
        out = await crawler._capture_homepage_screenshot(page, http_status=resp.status, final_url=page.url)
        assert out["status"] == "blocked" and "image_b64" not in out
    finally:
        await browser.close()
        await pw.stop()


# ── the scan result carries it, and old results without it still load ──────────────────────────────────────────────────────────────

def _result(shot=None):
    return ScanResult(domain="acme.test", root_url="https://acme.test/", scanner_version="x", consent_signals=ConsentSignalRecord(mechanism_type="none"), screenshot=shot)


def test_the_scan_result_carries_the_preview_through_json_as_a_windows_child_process_reports_it():
    rec = ScreenshotRecord(status="ok", image_b64=base64.b64encode(JPEG).decode(), width=1280, height=720, page_url="https://acme.test/", page_title="Acme")
    again = ScanResult.model_validate_json(_result(rec).model_dump_json())
    assert again.screenshot is not None and base64.b64decode(again.screenshot.image_b64) == JPEG


def test_a_scan_result_without_a_preview_is_still_valid():
    assert _result().screenshot is None
    assert ScanResult.model_validate_json(_result().model_dump_json()).screenshot is None


def test_the_preview_is_not_part_of_the_diagnostics_that_are_stored_with_the_scan():
    src = Path(crawler.__file__).read_text(encoding="utf-8")
    assert 'pre_consent_diagnostics.pop("homepage_screenshot", None)' in src             # a picture is not a diagnostic: removed before they are stored


# ── storage ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class FakeDb:
    def __init__(self, fail=False):
        self.added, self.fail, self.flushes = [], fail, 0

    def begin_nested(self):
        outer = self

        class Ctx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False        # re-raise, as a savepoint context does

        return Ctx()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        if self.fail:
            raise RuntimeError("database says no")
        self.flushes += 1


def save(db, rec):
    run(scan_repository._save_screenshot(db, uuid.uuid4(), rec))


def test_an_ok_preview_is_stored_as_the_image_bytes_with_its_size():
    db = FakeDb()
    save(db, ScreenshotRecord(status="ok", image_b64=base64.b64encode(JPEG).decode(), width=1280, height=720, page_url="https://acme.test/", page_title="Acme", http_status=200))
    (row,) = db.added
    assert isinstance(row, ScanScreenshot) and row.image == JPEG and (row.width, row.height, row.status, row.mime) == (1280, 720, "ok", "image/jpeg")


def test_a_preview_that_could_not_be_taken_is_stored_as_a_reason_with_no_image():
    db = FakeDb()
    save(db, ScreenshotRecord(status="blocked", reason="blocked", http_status=403, page_url="https://acme.test/", page_title="Access Denied"))
    (row,) = db.added
    assert row.status == "blocked" and row.image is None and row.mime is None and row.width is None


def test_nothing_is_stored_when_there_is_no_record():
    db = FakeDb()
    save(db, None)
    assert db.added == []


@pytest.mark.parametrize("rec", [
    ScreenshotRecord(status="ok", image_b64="not base64 !!", width=1, height=1),
    ScreenshotRecord(status="ok", image_b64=None, width=1, height=1),
    ScreenshotRecord(status="ok", image_b64=base64.b64encode(JPEG).decode(), width=None, height=None),
])
def test_a_broken_ok_preview_never_fails_the_scan_and_is_not_stored(rec):
    db = FakeDb()
    save(db, rec)                      # does not raise
    assert db.added == []


def test_a_database_error_while_storing_it_never_fails_the_scan():
    db = FakeDb(fail=True)
    save(db, ScreenshotRecord(status="ok", image_b64=base64.b64encode(JPEG).decode(), width=1280, height=720))   # does not raise


def test_the_model_and_the_migration_describe_the_same_table():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "0040_scan_screenshots.sql").read_text(encoding="utf-8")
    body = sql[sql.index("create table if not exists scan_screenshots"): sql.index(");", sql.index("create table if not exists scan_screenshots"))]
    columns = set(re.findall(r"^\s{4}(\w+)\s+(?:uuid|text|integer|bytea|timestamptz)\b", body, re.M))
    assert columns == {c.name for c in ScanScreenshot.__table__.columns}
    assert "unique references consent_scans(id) on delete cascade" in body                     # one per scan, gone with the scan
    assert "enable row level security" in sql and "tenant_isolation" in sql


# ── the endpoint ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

client = TestClient(app)
SCAN_ID = uuid.uuid4()


def _headers():
    token = jwt.encode({"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated", "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
                       os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _wire(monkeypatch, row, scan=True):
    from app.db.session import get_db

    async def fake_db():
        yield None

    async def get_scan(db, scan_id, org_id):
        return SimpleNamespace(website_id=uuid.uuid4()) if scan else None

    async def get_site(db, wid):
        return SimpleNamespace(domain="acme.test")

    async def get_shot(db, scan_id):
        return row

    monkeypatch.setattr(scan_repository, "get_scan", get_scan)
    monkeypatch.setattr(scan_repository, "get_website_by_id", get_site)
    monkeypatch.setattr(scan_repository, "get_screenshot", get_shot)
    app.dependency_overrides[get_db] = fake_db
    return get_db


def _row(**kw):
    base = dict(status="ok", reason=None, mime="image/jpeg", width=1280, height=720, page_url="https://acme.test/", image=JPEG, captured_at=datetime.now(UTC))
    return SimpleNamespace(**{**base, **kw})


def get(path="screenshot", scan=SCAN_ID):
    return client.get(f"/api/v1/consent/scans/{scan}/{path}", headers=_headers())


def test_the_endpoint_returns_the_stored_picture_with_the_scan_and_the_domain_it_belongs_to(monkeypatch):
    key = _wire(monkeypatch, _row())
    try:
        r = get()
        body = r.json()
        assert r.status_code == 200 and body["status"] == "ok" and body["available"] is True
        assert base64.b64decode(body["data"]) == JPEG and body["mime"] == "image/jpeg" and (body["width"], body["height"]) == (1280, 720)
        assert body["scan_id"] == str(SCAN_ID) and body["domain"] == "acme.test" and body["page_url"] == "https://acme.test/"
        assert body["message"] is None
    finally:
        app.dependency_overrides.pop(key, None)


@pytest.mark.parametrize("status,reason,needle", [
    ("blocked", "blocked", "did not show its homepage"), ("timeout", "capture_timeout", "in time"),
    ("failed", "capture_failed", "could not be taken"), ("failed", "blank", "blank"),
])
def test_a_preview_that_could_not_be_taken_says_so_plainly_and_carries_no_picture(monkeypatch, status, reason, needle):
    key = _wire(monkeypatch, _row(status=status, reason=reason, image=None, mime=None, width=None, height=None))
    try:
        body = get().json()
        assert body["available"] is False and body["data"] is None and needle in body["message"]
        assert body["status"] == status and body["domain"] == "acme.test"
        text = body["message"]
        assert not re.search(r"Traceback|Exception|/home|C:\\|\.py|capture_|playwright", text, re.I)      # nothing internal, no paths
    finally:
        app.dependency_overrides.pop(key, None)


def test_a_scan_made_before_previews_existed_says_so(monkeypatch):
    key = _wire(monkeypatch, None)
    try:
        body = get().json()
        assert body["status"] == "not_captured" and body["available"] is False and "before previews were added" in body["message"]
    finally:
        app.dependency_overrides.pop(key, None)


def test_the_endpoint_is_scoped_to_the_callers_organisation_and_needs_a_token(monkeypatch):
    key = _wire(monkeypatch, _row(), scan=False)
    try:
        assert get().status_code == 404
        assert client.get(f"/api/v1/consent/scans/{SCAN_ID}/screenshot").status_code == 401
        assert client.get("/api/v1/consent/scans/not-a-uuid/screenshot", headers=_headers()).status_code == 422
    finally:
        app.dependency_overrides.pop(key, None)
