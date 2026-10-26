"""The PDF report API: GET /api/v1/consent/scans/{id}/report.pdf and the service that prints the web report page.

The route's own rules (who may download, which scans have a report, what a failure looks like) are tested with the printing step replaced by a stand-in,
so they need no browser. The printing step itself is tested with a real headless Chromium against a small local page that behaves like the report page
(it sets the same window flags), so the page header, the footer with page numbers, the repeating table header and the multi-page output are checked in
a real PDF.
"""

import asyncio
import io
import os
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.api.v1.routes import consent_scans
from app.config import get_settings
from app.core.exceptions import ReportGenerationError
from app.db.repositories import scan_repository
from app.main import app
from app.scoring.dpdp_score import DISCLAIMER
from app.services import report_cache_service
from app.services import report_pdf_service as svc

client = TestClient(app)
SCAN_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
FAKE_PDF = b"%PDF-1.4\n% a stand-in\n%%EOF\n"


def _token(org: uuid.UUID = ORG_ID) -> str:
    return jwt.encode(
        {"sub": str(USER_ID), "org_id": str(org), "aud": "authenticated", "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
        os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256",
    )


def _headers(token: str | None = None) -> dict:
    return {"Authorization": f"Bearer {token or _token()}"}


# ── the page header and footer ────────────────────────────────────────────────────────────────────────────────────


def test_the_header_carries_the_logo_the_domain_and_the_scan_time_and_escapes_what_it_is_given():
    header = svc.build_header_template('evil.test"><script>alert(1)</script>', "Scanned 21 Oct 2026, 06:41 UTC · 5 of 520 pages scanned", "data:image/png;base64,AAAA")
    assert "data:image/png;base64,AAAA" in header and "linear-gradient" in header and "5 of 520 pages scanned" in header
    assert "<script>" not in header and "&lt;script&gt;" in header                       # a domain can never inject markup into the header


def test_the_header_still_prints_without_a_logo():
    assert "Consiva.ai" in svc.build_header_template("x.test", "Scanned", "")


def test_the_real_logo_is_shipped_with_the_application():
    assert svc._logo_data_uri().startswith("data:image/png;base64,") and len(svc._logo_data_uri()) > 1000


def test_the_footer_carries_the_disclaimer_and_the_page_number_out_of_the_count():
    footer = svc.build_footer_template()
    assert 'class="pageNumber"' in footer and 'class="totalPages"' in footer
    assert "for informational purposes only and does not constitute legal advice" in footer
    assert DISCLAIMER.split(" ")[0] in footer


def test_a_file_name_is_plain_characters_ending_in_pdf():
    sid = str(SCAN_ID)
    assert svc.safe_filename("consiva-privacy-report-x.test-2026-10-23.pdf", sid) == "consiva-privacy-report-x.test-2026-10-23.pdf"
    assert svc.safe_filename("../../etc/passwd", sid) == "etc-passwd.pdf"
    assert svc.safe_filename('a b"c;d\r\nX: y', sid) == "a-b-c-d-X-y.pdf"
    assert svc.safe_filename(None, sid) == f"consiva-privacy-report-{sid[:8]}.pdf"
    assert svc.safe_filename("   ", sid).endswith(".pdf")


def test_the_token_reaches_the_page_as_data_not_as_script():
    script = svc._storage_script('tok"; alert(1); //', "u", "o", None)
    assert "alert(1)" in script and '\\"' in script                                      # quoted and escaped by json.dumps
    assert script.count("sessionStorage.setItem") == 2 and "consiva.access_token" in script and "consiva.profile" in script


# ── the cache and the cap on browsers ───────────────────────────────────────────────────────────────────────────


def _count_renders(monkeypatch) -> list:
    calls: list = []

    async def fake_render(scan_id, token, user_id, org_id, role, settings, variant="free"):
        calls.append((scan_id, variant))
        return svc.RenderedReport(pdf=FAKE_PDF, filename="x.pdf")

    monkeypatch.setattr(svc, "_render", fake_render)
    svc.clear_cache()
    return calls


def _settings(**over):
    return get_settings().model_copy(update=over)


def _render(version="v1", settings=None, scan=SCAN_ID, variant="free"):
    return asyncio.run(svc.render_report_pdf(scan, version=version, token="t", user_id="u", org_id="o", role=None, settings=settings or _settings(), variant=variant))


def test_the_same_scan_is_printed_once_inside_the_cache_window_and_again_after_a_rerun(monkeypatch):
    calls = _count_renders(monkeypatch)
    # A fresh loop each time is what asyncio.run does; the cache must not depend on the loop.
    _render("v1"), _render("v1")
    assert len(calls) == 1
    _render("v2")                                                                          # the scan completed again: a new document
    assert len(calls) == 2
    _render("v1", scan=uuid.uuid4())                                                       # another scan is never answered from this one's copy
    assert len(calls) == 3


def test_each_report_type_is_cached_on_its_own_and_an_unknown_one_is_refused(monkeypatch):
    calls = _count_renders(monkeypatch)
    _render("v1", variant="free"), _render("v1", variant="free")
    _render("v1", variant="full"), _render("v1", variant="full")
    assert [v for _, v in calls] == ["free", "full"]                                       # a Free copy is never served for a Full request
    with pytest.raises(ReportGenerationError):
        _render("v1", variant="enterprise")
    assert len(calls) == 2


def test_a_cache_window_of_zero_prints_every_time(monkeypatch):
    calls = _count_renders(monkeypatch)
    s = _settings(report_pdf_cache_seconds=0)
    _render("v1", s), _render("v1", s)
    assert len(calls) == 2


def test_an_unexpected_failure_becomes_a_client_safe_error_with_no_internals(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret path /srv/app/token=abc")

    monkeypatch.setattr(svc, "_render", boom)
    svc.clear_cache()
    with pytest.raises(ReportGenerationError) as err:
        _render("v-fail")
    assert "secret" not in str(err.value) and "/srv" not in str(err.value) and "try again" in str(err.value)


def test_a_web_app_address_that_is_not_http_is_refused_before_a_browser_starts():
    with pytest.raises(ReportGenerationError):
        asyncio.run(svc._render(str(SCAN_ID), "t", "u", "o", None, _settings(report_frontend_url="file:///etc/passwd")))


# ── the route ───────────────────────────────────────────────────────────────────────────────────────────────────


def _wire(monkeypatch, *, found=True, status="completed", website_org=ORG_ID, render=None):
    from app.db.session import get_db

    class FakeDb:
        async def rollback(self):
            return None

    async def fake_db():
        yield FakeDb()

    # The route answers from the stored report when it is current (services/report_cache_service.py). These tests are about the route's own rules and
    # the printing hand-off, so nothing is ever stored and every request is printed; the cache has its own tests (test_report_cache.py).
    async def no_fingerprint(db, scan, settings=None):
        return "test"

    async def nothing_stored(db, scan_id, variant, current):
        return None

    async def store_nothing(*a, **k):
        return None

    monkeypatch.setattr(report_cache_service, "fingerprint", no_fingerprint)
    monkeypatch.setattr(report_cache_service, "get_stored", nothing_stored)
    monkeypatch.setattr(report_cache_service, "_store", store_nothing)
    report_cache_service._inflight.clear()

    async def get_scan(db, scan_id, org_id):
        return SimpleNamespace(id=scan_id, org_id=org_id, website_id=uuid.uuid4(), status=status, completed_at=datetime(2026, 10, 22, 12, 0, tzinfo=UTC)) if found else None

    async def get_site(db, wid):
        return SimpleNamespace(org_id=website_org, domain="acme.test")

    seen: dict = {}

    async def fake_render(scan_id, **kw):
        seen["scan_id"], seen["kw"] = scan_id, kw
        if render:
            return await render()
        return svc.RenderedReport(pdf=FAKE_PDF, filename="consiva-privacy-report-acme.test-2026-10-23.pdf")

    monkeypatch.setattr(scan_repository, "get_scan", get_scan)
    monkeypatch.setattr(scan_repository, "get_website_by_id", get_site)
    monkeypatch.setattr(consent_scans.report_pdf_service, "render_report_pdf", fake_render)
    app.dependency_overrides[get_db] = fake_db
    return get_db, seen


def _get(token: str | None = None, scan=SCAN_ID, query: str = ""):
    return client.get(f"/api/v1/consent/scans/{scan}/report.pdf{query}", headers=_headers(token))


def test_a_completed_scan_downloads_as_a_pdf_with_the_right_headers(monkeypatch):
    key, seen = _wire(monkeypatch)
    try:
        tok = _token()
        r = _get(tok)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        assert r.headers["content-disposition"] == 'attachment; filename="consiva-privacy-report-acme.test-2026-10-23.pdf"'
        assert r.headers["cache-control"] == "private, no-store" and r.headers["x-content-type-options"] == "nosniff"
        # The caller's own token and identity go to the printer, and nothing else is handed over.
        assert seen["scan_id"] == SCAN_ID and seen["kw"]["token"] == tok
        assert seen["kw"]["org_id"] == str(ORG_ID) and seen["kw"]["user_id"] == str(USER_ID) and seen["kw"]["version"] == "test"   # the printer is told the state of the report: the cache's fingerprint, not just the completion date
        assert tok not in r.text and tok not in str(r.headers)
    finally:
        app.dependency_overrides.pop(key, None)


def test_another_organisations_scan_is_not_found_and_nothing_is_printed(monkeypatch):
    key, seen = _wire(monkeypatch, found=False)
    try:
        r = _get()
        assert r.status_code == 404 and "scan_id" not in seen
    finally:
        app.dependency_overrides.pop(key, None)


def test_a_scan_whose_website_belongs_to_another_organisation_is_not_found(monkeypatch):
    key, seen = _wire(monkeypatch, website_org=uuid.uuid4())
    try:
        assert _get().status_code == 404 and "scan_id" not in seen
    finally:
        app.dependency_overrides.pop(key, None)


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_a_scan_that_has_not_completed_has_no_report(monkeypatch, status):
    key, seen = _wire(monkeypatch, status=status)
    try:
        r = _get()
        assert r.status_code == 409 and "not completed" in r.json()["detail"] and "scan_id" not in seen
    finally:
        app.dependency_overrides.pop(key, None)


def test_a_failure_to_print_is_a_503_with_a_plain_message_and_no_internals(monkeypatch):
    async def fail():
        raise ReportGenerationError("The PDF report could not be prepared. Please try again in a moment.")

    key, _ = _wire(monkeypatch, render=fail)
    try:
        r = _get()
        assert r.status_code == 503 and "could not be prepared" in r.json()["detail"]
        assert "Traceback" not in r.text and "playwright" not in r.text.lower()
    finally:
        app.dependency_overrides.pop(key, None)


def test_the_route_needs_a_token_and_a_valid_id(monkeypatch):
    key, seen = _wire(monkeypatch)
    try:
        assert client.get(f"/api/v1/consent/scans/{SCAN_ID}/report.pdf").status_code == 401
        assert client.get(f"/api/v1/consent/scans/{SCAN_ID}/report.pdf", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
        assert client.get("/api/v1/consent/scans/not-a-uuid/report.pdf", headers=_headers()).status_code == 422
        assert "scan_id" not in seen
    finally:
        app.dependency_overrides.pop(key, None)


@pytest.mark.parametrize("query,expected", [("", "long"), ("?variant=free", "free"), ("?variant=full", "full"), ("?variant=long", "long")])
def test_the_route_prints_the_report_type_asked_for_and_the_long_one_by_default(monkeypatch, query, expected):
    key, seen = _wire(monkeypatch)
    try:
        assert _get(_token(), query=query).status_code == 200
        assert seen["kw"]["variant"] == expected
    finally:
        app.dependency_overrides.pop(key, None)


def test_an_unknown_report_type_is_a_validation_error_not_a_server_error(monkeypatch):
    key, seen = _wire(monkeypatch)
    try:
        assert _get(_token(), query="?variant=enterprise").status_code == 422
        assert "scan_id" not in seen
    finally:
        app.dependency_overrides.pop(key, None)


def test_the_route_is_in_the_apis_own_document_as_a_pdf_response():
    op = app.openapi()["paths"]["/api/v1/consent/scans/{scan_id}/report.pdf"]["get"]
    assert "application/pdf" in op["responses"]["200"]["content"]


# ── a real browser prints a real PDF ────────────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fake report</title>
<style>body{font-family:Arial;margin:0;padding:0 34px} .row{height:40px} thead{display:table-header-group} tr{break-inside:avoid}
.band{background:#7a4dff;color:#fff;height:20px}</style></head><body>
<h1>Fake report</h1><p id="who"></p><p id="ext"></p>
<table><thead><tr><th>REPEATING-HEAD</th></tr></thead><tbody id="rows"></tbody></table>
<script>
  document.getElementById('who').textContent = 'TOKEN:' + (sessionStorage.getItem('consiva.access_token') || 'none') + ' PROFILE:' + (sessionStorage.getItem('consiva.profile') || 'none');
  const rows = document.getElementById('rows');
  for (let i = 1; i <= 120; i++) { const tr = document.createElement('tr'); tr.innerHTML = '<td class="row">Row number ' + i + '</td>'; rows.appendChild(tr); }
  fetch('http://external.invalid/leak').then(() => { document.getElementById('ext').textContent = 'EXTERNAL-REACHED'; }).catch(() => { document.getElementById('ext').textContent = 'EXTERNAL-REFUSED'; })
    .finally(() => { window.__REPORT_META__ = {domain: 'acme.test', scanned: 'Scanned 21 Oct 2026, 06:41 UTC · 5 of 520 pages scanned', filename: 'consiva-privacy-report-acme.test-2026-10-23.pdf'}; window.__REPORT_READY__ = true; });
</script></body></html>"""

SHEETS = """<!doctype html><html><head><meta charset="utf-8"><style>@page{size:A4;margin:0} body{margin:0}
.pg{width:210mm;height:296.6mm;overflow:hidden;break-after:page;position:relative;background:#fff} .pg:last-child{break-after:auto}</style></head><body>
<section class="pg"><h1>SHEET-ONE</h1></section><section class="pg"><h1>SHEET-TWO</h1></section>
<script>window.__REPORT_META__ = {domain: 'acme.test', scanned: 'Scanned', filename: 'consiva-privacy-report-acme.test-2026-10-23.pdf'}; window.__REPORT_READY__ = true;</script></body></html>"""


RULES = """<style>
html[data-print-part="sheets"] .rpt{display:none!important} html[data-print-part="flow"] .pg-doc{display:none!important}
.pg-doc{margin:0 -34px} .pg{width:210mm;height:296.6mm;overflow:hidden;break-after:page;background:#fff}</style>"""


def _combined(sheets: str) -> str:
    """The page the real app serves with no part asked for: the fixed sheets and the flowing report together, told apart by data-print-part."""
    doc = f'<div class="pg-doc"><style data-sheet-page="">@page{{size:A4;margin:0}}</style>{sheets}</div><div class="rpt"><h1>Fake report</h1>'
    return PAGE.replace("<style>", RULES + "<style>", 1).replace("<h1>Fake report</h1>", doc, 1).replace("</body>", "</div></body>")


COMBINED_LONG = _combined('<section class="pg"><h1>COVER-SHEET</h1></section>')
COMBINED_FULL = _combined('<section class="pg"><h1>SHEET-ONE</h1></section><section class="pg"><h1>SHEET-TWO</h1></section>')


class _VariantHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = COMBINED_FULL if "variant=full" in self.path else SHEETS if "part=pages" in self.path else COMBINED_LONG
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


FAILING_PAGE = "<!doctype html><title>x</title><script>window.__REPORT_ERROR__ = 'This scan did not complete'</script>"


class _Handler(BaseHTTPRequestHandler):
    body = PAGE

    def do_GET(self):
        data = type(self).body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(body: str):
    handler = type("H", (_Handler,), {"body": body})
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _serve_variants():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _VariantHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:  # noqa: BLE001
        return False


needs_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not installed here")


def _print_through_the_service(base: str, *, token: str, user: str, org: str, role: str | None, timeout: int, variant: str = "long") -> "svc.RenderedReport":
    """Prints through the same public entry point the route uses, so the test takes the same path as production (on Windows that path gives the browser an
    event loop of its own, which a test that called the private renderer in whatever loop is current would not)."""
    return asyncio.run(svc.render_report_pdf(
        uuid.uuid4(), version="test", token=token, user_id=user, org_id=org, role=role,
        settings=_settings(report_frontend_url=base, report_pdf_timeout_seconds=timeout, report_pdf_cache_seconds=0), variant=variant,
    ))


@needs_chromium
def test_a_real_browser_prints_a_multi_page_pdf_with_the_header_footer_and_repeating_table_header():
    server, base = _serve_variants()
    try:
        out = _print_through_the_service(base, token="the-callers-token", user="user-1", org="org-1", role="admin", timeout=60)
    finally:
        server.shutdown()
    assert out.pdf.startswith(b"%PDF") and out.filename == "consiva-privacy-report-acme.test-2026-10-23.pdf"
    full = PdfReader(io.BytesIO(out.pdf))
    assert "COVER-SHEET" in full.pages[0].extract_text(), "the long report opens with the cover sheet"
    assert "REPEATING-HEAD" not in full.pages[0].extract_text() and "informational purposes only" not in full.pages[0].extract_text()   # the cover has its own footer
    # the rest is checked as it always was: from the first page after the cover
    writer = __import__("pypdf").PdfWriter()
    for pg in full.pages[1:]:
        writer.add_page(pg)
    rest = io.BytesIO()
    writer.write(rest)
    reader = PdfReader(io.BytesIO(rest.getvalue()))
    assert len(reader.pages) >= 2, "120 rows must run onto more than one page"
    texts = [p.extract_text() for p in reader.pages]
    total = len(reader.pages)
    for i, t in enumerate(texts, 1):
        assert "acme.test" in t and "5 of 520 pages scanned" in t, f"header missing on page {i}"          # the header repeats on every page
        assert f"Page {i} of {total}" in t.replace("\n", " ") or f"{i} of {total}" in t.replace("\n", " "), f"page number missing on page {i}"
        assert "informational purposes only" in t, f"disclaimer missing on page {i}"
        assert "REPEATING-HEAD" in t, f"table header not repeated on page {i}"
    # The header band starts at the top edge of every page and ends above where the content starts, so nothing is hidden under it.
    for i, page in enumerate(reader.pages, 1):
        height = float(page.mediabox.height)
        found: list[tuple[float, str]] = []

        def visit(text, cm, tm, font_dict, font_size, found=found, height=height):
            if text.strip():
                found.append((height - (cm[3] * tm[5] + cm[5]), text.strip()))   # distance from the top edge of the page, in points

        page.extract_text(visitor_text=visit)
        header = [d for d, txt in found if "acme.test" in txt or "pages scanned" in txt]
        assert header and all(d < 45 for d in header), f"header text is not in the band at the top of page {i}: {header}"
        body = [d for d, txt in found if "acme.test" not in txt and "pages scanned" not in txt and d < height - 70]   # not the header, not the footer
        assert body and min(body) > 49, f"content runs under the header on page {i} (starts {min(body):.0f}pt from the top)"
    first = texts[0]
    assert "TOKEN:the-callers-token" in first and '"user_id": "user-1"' in first.replace("\n", " ") or "user-1" in first.replace("\n", " ")   # the caller's own session reached the page
    assert "EXTERNAL-REFUSED" in first and "EXTERNAL-REACHED" not in first                                # nothing but the configured web app may be reached
    rows = [int(x) for t in texts for x in __import__("re").findall(r"Row number (\d+)", t)]
    assert rows == list(range(1, 121)), "every row is printed once, in order"


@needs_chromium
def test_a_page_that_says_it_cannot_draw_the_report_becomes_a_clean_error_not_a_timeout():
    server, base = _serve(FAILING_PAGE)
    try:
        with pytest.raises(ReportGenerationError) as err:
            _print_through_the_service(base, token="t", user="u", org="o", role=None, timeout=30)
    finally:
        server.shutdown()
    assert "could not be prepared" in str(err.value) and "did not complete" not in str(err.value)


@needs_chromium
def test_the_free_report_prints_exactly_its_fixed_sheets_on_a4_with_no_added_header_or_footer():
    server, base = _serve_variants()
    try:
        out = _print_through_the_service(base, token="t", user="u", org="o", role=None, timeout=60, variant="free")
    finally:
        server.shutdown()
    reader = PdfReader(io.BytesIO(out.pdf))
    assert len(reader.pages) == 2, "two sheets make two pages, with no blank page after them"
    assert out.filename == "consiva-privacy-report-acme.test-2026-10-23-free.pdf"
    for page, word in zip(reader.pages, ("SHEET-ONE", "SHEET-TWO")):
        assert word in page.extract_text()
        assert abs(float(page.mediabox.width) - 595) < 2 and abs(float(page.mediabox.height) - 842) < 2          # A4
        assert "informational purposes only" not in page.extract_text()                                           # the sheets carry their own footer


@needs_chromium
def test_the_full_report_is_its_fixed_sheets_followed_by_the_appendix_in_one_pdf():
    server, base = _serve_variants()
    try:
        out = _print_through_the_service(base, token="t", user="u", org="o", role=None, timeout=90, variant="full")
    finally:
        server.shutdown()
    reader = PdfReader(io.BytesIO(out.pdf))
    texts = [p.extract_text() for p in reader.pages]
    assert "SHEET-ONE" in texts[0] and "SHEET-TWO" in texts[1], "the fixed sheets come first"
    assert len(reader.pages) > 2 and "Row number 1" in texts[2], "then the appendix"
    assert "Appendix page 1 of" in texts[2].replace("\n", " "), "the appendix numbers its own pages"
    assert out.filename.endswith("-full.pdf")
